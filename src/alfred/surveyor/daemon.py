"""Main loop orchestrator — watch → embed → cluster → label → write-back."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from .clusterer import Clusterer
from .config import PipelineConfig
from .embedder import Embedder
from .labeler import Labeler
from .parser import parse_file, VaultRecord
from .state import PipelineState
from .utils import compute_md5
from .watcher import VaultWatcher
from .writer import VaultWriter

log = structlog.get_logger()

LOOP_INTERVAL = 5.0  # seconds


class Daemon:
    def __init__(self, cfg: PipelineConfig) -> None:
        self.cfg = cfg
        self._shutdown_requested = False

        self.state = PipelineState(cfg.state.path)
        self.watcher = VaultWatcher(cfg.vault, cfg.watcher)
        self.embedder = Embedder(cfg.ollama, cfg.milvus, cfg.vault.path, self.state)
        self.clusterer = Clusterer(cfg.clustering, self.state)
        self.labeler = Labeler(cfg.openrouter, cfg.labeler)
        self.writer = VaultWriter(cfg.vault.path, self.state)

    def request_shutdown(self) -> None:
        log.info("daemon.shutdown_requested")
        self._shutdown_requested = True

    async def shutdown(self) -> None:
        """Graceful shutdown: stop watcher, close HTTP clients, save state."""
        log.info("daemon.shutting_down")
        self.watcher.stop()
        await self.embedder.close()
        self.state.save()
        log.info("daemon.shutdown_complete")

    async def run(self) -> None:
        """Main daemon loop."""
        log.info("daemon.starting")
        self.state.load()

        # Purge any pre-existing state + Milvus rows for paths that now fall
        # under ignore_dirs. Without this, stale entries from a prior config
        # would keep appearing in cluster memberships and getting re-labeled,
        # driving continuous re-writes of session/inbox files.
        self._purge_ignored_paths()

        # Initial sync if no state exists
        if not self.state.files:
            await self._initial_sync()

        # Start filesystem watcher
        self.watcher.start()

        try:
            while not self._shutdown_requested:
                await self._tick()
                await asyncio.sleep(LOOP_INTERVAL)
        finally:
            await self.shutdown()

    def _is_ignored(self, rel_path: str) -> bool:
        """Check whether a vault-relative path falls under any ignored directory."""
        parts = rel_path.split("/")
        ignore_dirs = set(self.cfg.vault.ignore_dirs)
        for part in parts[:-1]:
            if part in ignore_dirs:
                return True
        return False

    def _purge_ignored_paths(self) -> None:
        """Drop any state rows + Milvus rows whose path is now under ignore_dirs.

        Needed when ignore_dirs is expanded in config: pre-existing embeddings
        from the prior config would otherwise keep appearing in cluster
        memberships and get re-labeled every tick, causing continuous rewrites.
        """
        # Purge from in-memory state
        ignored_state = [p for p in self.state.files if self._is_ignored(p)]
        for rel in ignored_state:
            self.state.remove_file(rel)

        # Purge from Milvus — collect all stored ids and delete any that match
        purged_milvus = 0
        embedding_data = self.embedder.get_all_embeddings()
        if embedding_data is not None:
            paths, _ = embedding_data
            for rel in paths:
                if self._is_ignored(rel):
                    try:
                        self.embedder.milvus.delete(
                            collection_name=self.embedder.collection_name,
                            filter=f'id == "{rel}"',
                        )
                        purged_milvus += 1
                    except Exception as e:
                        log.warning("daemon.purge_delete_error", path=rel, error=str(e))

        if ignored_state or purged_milvus:
            log.info(
                "daemon.purged_ignored_paths",
                state_removed=len(ignored_state),
                milvus_removed=purged_milvus,
            )
            self.state.save()

    async def _initial_sync(self) -> None:
        """Full scan → embed all → cluster → label."""
        log.info("daemon.initial_sync_start")

        hashes = self.watcher.full_scan()
        for rel, md5 in hashes.items():
            self.state.update_file(rel, md5)

        all_paths = list(hashes.keys())
        records = await self.embedder.process_diff(all_paths, [], [])

        # Need all records for clustering — parse any we didn't get from embedder
        all_records = dict(records)
        for rel in all_paths:
            if rel not in all_records:
                try:
                    all_records[rel] = parse_file(self.cfg.vault.path, rel)
                except Exception:
                    pass

        await self._cluster_and_label(all_records)
        self.state.save()
        log.info("daemon.initial_sync_complete", files=len(hashes))

    async def _tick(self) -> None:
        """One iteration of the main loop."""
        # Collect debounced file events
        debounced = self.watcher.collect_debounced()
        if not debounced:
            return

        # Compute hashes for touched files
        current_hashes: dict[str, str] = {}
        vault_path = self.cfg.vault.path
        for rel in debounced:
            full = vault_path / rel
            if full.exists():
                try:
                    current_hashes[rel] = compute_md5(full)
                except OSError:
                    pass

        # Also include all known files for a complete diff
        for rel, fs in self.state.files.items():
            if rel not in current_hashes:
                full = vault_path / rel
                if full.exists():
                    current_hashes[rel] = fs.md5
                # If file doesn't exist, it's not in current_hashes → will be detected as deleted

        diff = self.state.compute_diff(current_hashes)
        if diff.empty:
            return

        log.info("daemon.processing_diff", diff=str(diff))

        # Update state with new hashes
        for rel in diff.new + diff.changed:
            if rel in current_hashes:
                self.state.update_file(rel, current_hashes[rel])

        # Stage 2: Embed
        records = await self.embedder.process_diff(diff.new, diff.changed, diff.deleted)

        # Parse all known records for clustering
        all_records: dict[str, VaultRecord] = {}
        for rel in self.state.files:
            if rel in records:
                all_records[rel] = records[rel]
            else:
                try:
                    all_records[rel] = parse_file(self.cfg.vault.path, rel)
                except Exception:
                    pass

        # Stage 3 + 4: Cluster and label
        await self._cluster_and_label(all_records)
        self.state.save()

    async def _cluster_and_label(self, records: dict[str, VaultRecord]) -> None:
        """Run clustering then label changed clusters."""
        # Get all embeddings for clustering
        embedding_data = self.embedder.get_all_embeddings()
        if embedding_data is None:
            log.info("daemon.no_embeddings_to_cluster")
            return

        paths, vectors = embedding_data

        # Stage 3: Cluster
        result = self.clusterer.run(paths, vectors, records)

        # Stage 4: Label changed clusters
        all_changed = result.changed_semantic | result.changed_structural
        if not all_changed:
            log.info("daemon.no_changed_clusters")
            return

        # Build cluster membership map (semantic). Skip ignored paths so that
        # stale embeddings (e.g. session/ rows surviving a config change that
        # haven't been purged yet) cannot drive writebacks to files outside
        # the surveyor's scope.
        cluster_members: dict[int, list[str]] = {}
        for path, cid in result.semantic.items():
            if cid == -1:
                continue
            if self._is_ignored(path):
                continue
            cluster_members.setdefault(cid, []).append(path)

        for cid in all_changed:
            members = cluster_members.get(cid, [])
            if len(members) < self.cfg.labeler.min_cluster_size_to_label:
                continue

            # Label the cluster
            tags = await self.labeler.label_cluster(cid, members, records)
            if tags:
                # Write tags to all members
                for path in members:
                    self.writer.write_alfred_tags(path, tags)

                # Update cluster state
                cluster_key = f"semantic_{cid}"
                from .state import ClusterState
                from datetime import datetime, timezone
                self.state.clusters[cluster_key] = ClusterState(
                    label=tags,
                    member_files=members,
                    last_labeled=datetime.now(timezone.utc).isoformat(),
                )

            # Suggest relationships
            rels = await self.labeler.suggest_relationships(cid, members, records)
            for rel in rels:
                source = rel.get("source", "")
                if source in records:
                    self.writer.write_relationships(source, [rel])

        log.info("daemon.labeling_complete", clusters_processed=len(all_changed))
