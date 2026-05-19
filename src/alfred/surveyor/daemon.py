"""Main loop orchestrator — watch → embed → cluster → label → write-back."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from alfred.common.heartbeat import Heartbeat

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

# Fallback if watcher config is missing — matches WatcherConfig default.
# The actual per-tick sleep comes from self.cfg.watcher.debounce_seconds so
# the daemon wakes in step with the debounce window rather than spinning
# through useless ticks mid-debounce.
DEFAULT_LOOP_INTERVAL = 30.0
# How often to persist state during the labeling pass. Each label +
# suggest_relationships call costs 1-2 LLM round-trips; checkpointing
# every N clusters means a mid-pass kill loses at most this many
# clusters of work. 10 is a good balance — ~5s of extra disk I/O per
# full 200-cluster pass, minimal in the big picture.
LABEL_CHECKPOINT_EVERY = 10


class Daemon:
    def __init__(self, cfg: PipelineConfig) -> None:
        self.cfg = cfg
        self._shutdown_requested = False
        # Daemon-lifecycle gate for the entity-link no-entities-in-vault
        # observability log (Phase 5 Sub-arc B). Flipped to True after the
        # first emission; reset to False on any sweep that DOES find
        # entity records so a transition back to empty re-emits. Per
        # ``feedback_intentionally_left_blank.md``: silence-from-no-data
        # must surface distinctly from silence-from-failure; once-per-
        # lifecycle keeps the log signal low while remaining grep-able.
        self._entity_link_no_entities_logged: bool = False
        # Phase 5 Sub-arc D1 (2026-05-19): once-per-lifecycle latch for
        # the cluster→MOC suggestion stage when it's configured-off.
        # Salem + KAL-LE leave ``moc_suggestion.enabled`` at the False
        # default; without this gate every sweep would emit a noisy
        # ``stage_disabled`` log. Resets to False if a future config
        # reload flips the flag on (defensive; daemon doesn't currently
        # reload mid-process, but the field is cheap).
        self._moc_suggestion_disabled_logged: bool = False

        self.state = PipelineState(cfg.state.path)
        self.watcher = VaultWatcher(cfg.vault, cfg.watcher)
        self.embedder = Embedder(cfg.ollama, cfg.milvus, cfg.vault.path, self.state)
        self.clusterer = Clusterer(cfg.clustering, self.state)
        self.labeler = Labeler(cfg.openrouter, cfg.labeler)
        # Idle-tick heartbeat — see ``alfred.common.heartbeat`` for the
        # "intentionally left blank" rationale. Counter is bumped per
        # record returned from ``embedder.process_diff`` (one record
        # re-embedded = one event). Task spawn + asyncio.Event creation
        # live in :meth:`run` so the event is bound to the right loop.
        self.heartbeat: Heartbeat = Heartbeat(daemon_name="surveyor", log=log)
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_shutdown: asyncio.Event | None = None
        # Mirror the curator/janitor/distiller pattern: derive the unified
        # audit-log path from the state-file directory so every surveyor
        # write lands in data/vault_audit.log alongside the other tools.
        # Without this, drift investigations that grep the audit log for
        # who-touched-what see zero surveyor entries even though the
        # surveyor is the only daemon writing alfred_tags.
        audit_log_path = Path(cfg.state.path).parent / "vault_audit.log"
        self.writer = VaultWriter(cfg.vault.path, self.state, audit_log_path=audit_log_path)

    def request_shutdown(self) -> None:
        log.info("daemon.shutdown_requested")
        self._shutdown_requested = True

    async def shutdown(self) -> None:
        """Graceful shutdown: stop watcher, close HTTP clients, save state."""
        log.info("daemon.shutting_down")
        self.watcher.stop()
        # Stop the heartbeat task first so it doesn't keep firing
        # post-shutdown logs.
        if self._heartbeat_shutdown is not None:
            self._heartbeat_shutdown.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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

        # Always run a startup sync — this reconciles any drift between the
        # vault on disk and persisted state (files added while surveyor was
        # down never fire watcher events). compute_diff makes this cheap —
        # only truly-new-or-changed files get re-embedded.
        await self._startup_sync()

        # Start filesystem watcher
        self.watcher.start()

        # Idle-tick heartbeat task — emits ``surveyor.idle_tick`` every
        # ``cfg.idle_tick.interval_seconds``. Default 60s, on by default.
        # See ``alfred.common.heartbeat`` for the "intentionally left
        # blank" rationale. Spawned only when enabled — disabled path is
        # silent. Event creation deferred to here so it binds to the
        # right asyncio loop (Daemon.__init__ runs before asyncio.run()).
        if self.cfg.idle_tick.enabled:
            self._heartbeat_shutdown = asyncio.Event()
            self._heartbeat_task = asyncio.create_task(
                self.heartbeat.run(
                    interval_seconds=self.cfg.idle_tick.interval_seconds,
                    shutdown_event=self._heartbeat_shutdown,
                ),
                name="surveyor-heartbeat",
            )
            log.info(
                "daemon.heartbeat_started",
                interval_seconds=self.cfg.idle_tick.interval_seconds,
            )

        # Sleep at the debounce cadence: polling faster than the debounce
        # window just spins through ticks that find nothing to do.
        loop_interval = getattr(
            self.cfg.watcher, "debounce_seconds", DEFAULT_LOOP_INTERVAL
        )
        try:
            while not self._shutdown_requested:
                await self._tick()
                await asyncio.sleep(loop_interval)
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

    async def _startup_sync(self) -> None:
        """Reconcile full vault against persisted state on boot.

        Runs a full filesystem rescan, diffs against state.files, and
        embeds only what's actually new or changed. Deleted files are
        purged. Unlike the prior `_initial_sync`, this path is idempotent:
        a partially-complete previous run (e.g. killed mid-labeling by
        alfred-update.timer) resumes cleanly instead of redoing everything
        from scratch.

        Also handles drift for tenants whose state file is intact but
        stale (files added to the vault while surveyor was down — the
        watcher only fires inotify events after startup, so those files
        are otherwise invisible to `_tick`).
        """
        log.info("daemon.startup_sync_start")

        hashes = self.watcher.full_scan()
        diff = self.state.compute_diff(hashes)

        log.info(
            "daemon.startup_sync_diff",
            on_disk=len(hashes),
            in_state=len(self.state.files),
            new=len(diff.new),
            changed=len(diff.changed),
            deleted=len(diff.deleted),
        )

        # Update state md5s for new/changed files BEFORE embedding.
        for rel in diff.new + diff.changed:
            if rel in hashes:
                self.state.update_file(rel, hashes[rel])

        # Embed only the delta. On a cold start this is everything; on a
        # resumed-after-restart case this is often zero or a handful.
        records = await self.embedder.process_diff(diff.new, diff.changed, diff.deleted)

        # Checkpoint here so the ~2min (cloud) / ~30min (local) embed work
        # survives even if the labeling pass crashes or gets killed.
        self.state.save()
        log.info("daemon.startup_embed_checkpoint", embedded=len(records))

        # Parse records that weren't freshly embedded so clustering sees
        # the full vault in memory.
        all_records = dict(records)
        for rel in hashes:
            if rel not in all_records:
                try:
                    all_records[rel] = parse_file(self.cfg.vault.path, rel)
                except Exception:
                    pass

        # Entity records that appeared for the FIRST TIME (diff.new, not
        # diff.changed) trigger the backfill pass on the next
        # _cluster_and_label call.
        from .labeler import ENTITY_RECORD_TYPES
        new_entity_paths = [
            p for p in diff.new
            if all_records.get(p) is not None
            and all_records[p].record_type in ENTITY_RECORD_TYPES
        ]

        await self._cluster_and_label(all_records, newly_added_entity_paths=new_entity_paths)
        self.state.save()
        log.info("daemon.startup_sync_complete", files=len(hashes))

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
        # Idle-tick counter — one record re-embedded = one event.
        # ``process_diff`` returns the records that were actually
        # embedded (parse failures + empty-text skips are not in this
        # dict), so len(records) is the correct meaningful count.
        for _ in range(len(records)):
            self.heartbeat.record_event()

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

        # Entity records that are genuinely NEW (not just changed) drive
        # the Stage-7 backfill pass — a renamed or edited matter
        # shouldn't re-spray related_matters across 3000 events.
        from .labeler import ENTITY_RECORD_TYPES
        new_entity_paths = [
            p for p in diff.new
            if all_records.get(p) is not None
            and all_records[p].record_type in ENTITY_RECORD_TYPES
        ]

        # Stage 3 + 4 + 5 + 6 + 7: Cluster, label, entity-link, noise-link, backfill
        await self._cluster_and_label(all_records, newly_added_entity_paths=new_entity_paths)
        self.state.save()

    async def _cluster_and_label(
        self,
        records: dict[str, VaultRecord],
        newly_added_entity_paths: list[str] | None = None,
    ) -> None:
        """Run clustering then label changed clusters.

        If `newly_added_entity_paths` is supplied and entity_link.backfill_enabled
        is true, a Stage-7 backfill pass runs after cluster + noise linking:
        for each new entity, scan every non-entity record in the vault and
        write the link when similarity is above threshold. This is the
        path that gives brand-new matters / persons / orgs / projects an
        immediate structural footprint instead of waiting for the next
        clustering pass to co-cluster them with something.
        """
        # Get all embeddings for clustering
        embedding_data = self.embedder.get_all_embeddings()
        if embedding_data is None:
            log.info("daemon.no_embeddings_to_cluster")
            return

        paths, vectors = embedding_data

        # Stage 3: Cluster
        result = self.clusterer.run(paths, vectors, records)

        # Sub-arc B placement fix (2026-05-19): observability gate must
        # fire BEFORE the no_changed_clusters early-return, otherwise
        # the gate never emits on the exact vault it was designed for.
        # Hypatia's first sweep (81 records embedded, clusterer reports
        # changed_semantic=0 because the cluster state is stable) hit
        # ``daemon.no_changed_clusters`` and short-circuited the
        # function before reaching the original gate placement below
        # the labeling fan-out. The gate is evaluated here so it runs
        # regardless of whether stage 4 labeling has work to do; the
        # skip-stage-5/6/7 decision is consumed at the bottom of this
        # method where it would otherwise dispatch entity-link work.
        entities_present = self._gate_entity_link_no_entities_observability(
            records,
        )

        # Stage 4: Label changed clusters.
        #
        # Scope to `changed_semantic` only. `cluster_members` below is built
        # from `result.semantic` — structural cluster IDs have no member
        # paths in that map. If we include `changed_structural` in the
        # union, the numeric overlap between the two namespaces (both are
        # integers starting from 0) causes spurious re-labeling: a semantic
        # cluster whose semantic membership didn't change still gets
        # relabeled whenever a structural cluster with the same ID
        # changed. On David's vault that's a ~10x LLM-cost multiplier for
        # zero additional information — every "changed structural" was
        # hitting an unrelated semantic cluster's members.
        all_changed = set(result.changed_semantic)
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

        # Clusters are independent — their label calls and relationship
        # suggestions don't share state. Fan them out through an asyncio
        # semaphore so we can keep up to `max_concurrent` LLM calls in
        # flight instead of serialising 208 × 2 calls at ~5-10s each.
        from .state import ClusterState
        from datetime import datetime, timezone

        semaphore = asyncio.Semaphore(self.cfg.labeler.max_concurrent)
        # Checkpoint state every N clusters so a mid-pass SIGKILL (e.g. the
        # tenant's alfred-update.timer recreating the container every 15
        # min) loses at most `LABEL_CHECKPOINT_EVERY` clusters of labeling
        # work, not the whole pass.
        processed_counter = {"count": 0}

        async def _process_cluster(cid: int) -> None:
            members = cluster_members.get(cid, [])
            if len(members) < self.cfg.labeler.min_cluster_size_to_label:
                return

            # Membership-stability gate. HDBSCAN + Leiden re-run every tick
            # and renumber cluster IDs non-deterministically — so
            # ``all_changed`` (derived from ID diffs) flags clusters that
            # often have identical member sets vs their prior ``ClusterState``
            # snapshot. Hitting Ollama for those is wasted work:
            # ``writer.write_alfred_tags`` logs ``tags_unchanged`` because
            # content is stable. One vault write that triggered 19
            # attribution-marker updates on a single record cascaded into
            # ~80s of this pattern on 2026-04-23 22:29 ADT and OOM-killed
            # WSL. Gate both label_cluster and suggest_relationships on a
            # sorted-member-paths equality check against ``state.clusters``.
            # Both calls are keyed off cluster membership, so if membership
            # is stable neither needs re-running.
            membership_key = tuple(sorted(members))
            cluster_key_check = f"semantic_{cid}"
            prev = self.state.clusters.get(cluster_key_check)
            if prev is not None and tuple(sorted(prev.member_files)) == membership_key:
                log.info(
                    "daemon.membership_unchanged_skip",
                    cid=cid,
                    member_count=len(members),
                )
                return

            async with semaphore:
                tags = await self.labeler.label_cluster(cid, members, records)
                if tags:
                    # Per-record text-anchor gate (architectural twin to
                    # the entity-link gate shipped in db9392f, P0 from
                    # vault-reviewer 2026-05-05). Embedding clusters
                    # produce topic-coherent false positives — events
                    # cluster with music records on shared date/location
                    # signal even when no music event is the subject.
                    # Without per-record filtering, every member of a
                    # heterogeneous cluster gets every label (Halifax
                    # Music Fest, dental appointments, EI Call all
                    # tagged ``events/music``). The gate filters
                    # per-MEMBER, so different members may keep
                    # different filtered subsets of the cluster's
                    # proposed tag-set. Per-member writeback logic
                    # extracted to ``_writeback_one_member`` so each
                    # log path (tag_blocked, all_tags_blocked,
                    # tag_write_skipped_no_record) is unit-testable
                    # without driving the whole async cluster loop —
                    # closes WARN 1 from code-reviewer's verdict on
                    # 47b1b75.
                    for path in members:
                        self._writeback_one_member(
                            cid=cid,
                            path=path,
                            record=records.get(path),
                            proposed_tags=tags,
                        )
                    cluster_key = f"semantic_{cid}"
                    self.state.clusters[cluster_key] = ClusterState(
                        label=tags,
                        member_files=members,
                        last_labeled=datetime.now(timezone.utc).isoformat(),
                    )

                rels = await self.labeler.suggest_relationships(
                    cid, members, records
                )
                for rel in rels:
                    source = rel.get("source", "")
                    if source in records:
                        self.writer.write_relationships(source, [rel])

                processed_counter["count"] += 1
                if processed_counter["count"] % LABEL_CHECKPOINT_EVERY == 0:
                    self.state.save()
                    log.info(
                        "daemon.label_checkpoint",
                        completed=processed_counter["count"],
                        total=len(all_changed),
                    )

        # gather() with return_exceptions=True keeps the pass running even
        # if one cluster's LLM call blows up — we'd rather log and keep
        # labeling the rest than abort the whole surveyor tick.
        results = await asyncio.gather(
            *(_process_cluster(cid) for cid in all_changed),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, Exception)]
        for err in failures:
            log.warning("daemon.cluster_label_error", error=str(err)[:200])

        # Stage 8 (Phase 5 Sub-arc D1): cluster→MOC suggestion emission.
        # Walks each labeled cluster and proposes which MOC(s) its
        # members might belong to. Pure read of vault + record map +
        # MOC index; persists to an out-of-vault JSONL queue. NEVER
        # writes to the vault. The accept path lives in D2 (slash
        # commands).
        #
        # Placed BEFORE the entity-link skip-gate because the
        # suggestion stage doesn't depend on entity records — Hypatia
        # (no entity types) needs it, Salem (entity-heavy) would also
        # benefit but is currently disabled-by-default config.
        suggestions_proposed = await self._maybe_emit_moc_suggestions(
            cluster_members=cluster_members,
            all_changed=all_changed,
            records=records,
        )

        log.info(
            "daemon.labeling_complete",
            clusters_processed=len(all_changed),
            failed=len(failures),
            concurrency=self.cfg.labeler.max_concurrent,
            moc_suggestions_proposed=suggestions_proposed,
        )

        # Stage 5/6/7 skip — consumes the gate result evaluated right
        # after clustering (above the no_changed_clusters early-return).
        # The gate already emitted its observability log (or held the
        # latch) on the way in; this is purely the consumer-side
        # decision to skip stage 5/6/7 when entities are absent.
        if not entities_present:
            return

        # Stage 5: structured entity-link writeback. For each cluster whose
        # membership changed, walk non-entity members and add typed
        # frontmatter links (related_matters / related_persons / related_orgs
        # / related_projects) to any entity member of the same cluster whose
        # cosine similarity is above the configured threshold.
        self._link_entities_in_clusters(
            all_changed, cluster_members, records, paths, vectors,
        )

        # Stage 6: noise-point entity linking. HDBSCAN assigns cluster id -1
        # to records that don't fit any cluster. Those records still deserve
        # related_* frontmatter — we just can't infer it from cluster
        # co-membership. Instead, compare each noise point directly against
        # every entity record in the vault and link above threshold.
        noise_paths = [p for p, cid in result.semantic.items() if cid == -1]
        if noise_paths:
            self._link_noise_points_to_entities(
                noise_paths, records, paths, vectors,
            )

        # Stage 7: backfill links FROM every non-entity record in the vault
        # TO each newly-created entity. This is the complement of the cluster
        # + noise passes, which link FROM a record TO nearby entities — here
        # we link FROM a new entity OUT to every record it's close to, so a
        # matter added at 3pm has its related-* footprint by 3:01pm rather
        # than waiting for the next cluster membership shift.
        if newly_added_entity_paths and self.cfg.entity_link.backfill_enabled:
            self._backfill_new_entities(
                newly_added_entity_paths, records, paths, vectors,
            )

    # ---- Per-write text-anchor gate (Phase 1 source-side fix 2026-05-03) ----
    #
    # Combines cosine similarity (which the three stages below already
    # gate on) with mention detection (the entity's display name must
    # appear as a word-boundary match in the source record's text
    # surfaces). Standard precision-control pattern from entity-linking
    # systems — pure cosine threshold underperforms because topic-
    # coherent embeddings cluster without factual association.
    #
    # Reuses the cleanup module's word-boundary regex helper so the
    # source-side gate and the bulk-cleanup CLI use byte-identical
    # logic. Without that parity, a record could pass the source gate
    # but fail the cleanup heuristic (or vice versa), creating a
    # second contamination class.
    #
    # The helper expects an in-memory ``VaultRecord`` (which the
    # daemon has already parsed); reading frontmatter+body off disk
    # again would double the I/O for no semantic gain.

    def _writeback_one_member(
        self,
        *,
        cid: int,
        path: str,
        record: "VaultRecord | None",
        proposed_tags: list[str],
    ) -> None:
        """Apply the per-record text-anchor gate + write the filtered
        tag-set for one cluster member. Extracted from
        ``_process_cluster``'s loop so each log path is unit-testable
        without driving the whole async cluster pipeline (WARN 1
        from code-reviewer on 47b1b75).

        Three log paths, all observable per
        ``feedback_intentionally_left_blank.md``:
          * ``surveyor.tag_write_skipped_no_record`` — fires when
            ``record is None`` (transient or skipped). Defensive: do
            not fall through to the legacy unfiltered write because
            that's the contamination shape.
          * ``surveyor.tag_blocked_no_text_anchor`` — fires once per
            tag dropped by the per-record gate. Operator greps to
            see which proposed labels the gate rejected on which
            records.
          * ``surveyor.all_tags_blocked`` — fires when EVERY proposed
            tag fails the anchor check (heterogeneous-cluster
            signal). Surfaces clusters that produced zero anchored
            matches so an operator can review HDBSCAN parameters or
            the labeler prompt for that shape.

        Writes only the anchored subset. No-op (with log) when the
        whole proposed list is filtered out.
        """
        if record is None:
            log.info(
                "surveyor.tag_write_skipped_no_record",
                record_path=path,
                cluster_id=cid,
                proposed_count=len(proposed_tags),
            )
            return
        anchored_tags = self._filter_anchored_tags(proposed_tags, record)
        blocked_tags = [t for t in proposed_tags if t not in anchored_tags]
        for blocked in blocked_tags:
            log.info(
                "surveyor.tag_blocked_no_text_anchor",
                record_path=path,
                tag=blocked,
                cluster_id=cid,
            )
        if anchored_tags:
            self.writer.write_alfred_tags(path, anchored_tags)
        elif proposed_tags:
            log.info(
                "surveyor.all_tags_blocked",
                record_path=path,
                cluster_id=cid,
                proposed_count=len(proposed_tags),
                proposed_tags=proposed_tags,
            )

    def _filter_anchored_tags(
        self,
        proposed_tags: list[str],
        record: "VaultRecord",
    ) -> list[str]:
        """Return the subset of ``proposed_tags`` whose anchor terms
        appear as word-boundary matches in the record's corpus.

        Returns ``proposed_tags`` unchanged when:
          * ``labeler.require_text_anchor`` is False (legacy cosine-
            only contract — opt-out for tests + downstream workflows
            that explicitly want untrusted tag application).
          * ``proposed_tags`` is empty.

        Otherwise iterates per-tag and keeps only those whose anchor
        term (last segment after splitting on ``/`` then ``-``) has
        word-boundary textual presence in the record body / title /
        description / related list / relationships array's anchor
        strings.

        Architectural twin to ``_entity_name_appears_in_record``
        (db9392f); same predicate, different extraction. Caller
        emits ``surveyor.tag_blocked_no_text_anchor`` per dropped
        tag and ``surveyor.all_tags_blocked`` when the filtered set
        is empty (per ``feedback_intentionally_left_blank.md``).

        Reuses cleanup module helpers (``_tag_anchored_in_corpus`` +
        ``_build_record_corpus``) for byte-identical parity with the
        Phase 2 cleanup CLI that will scrub historical contamination
        — mismatched semantics would either over-remove operator-
        curated tags or under-remove cluster-bleed contamination.
        Local import inside the method so the cleanup module's tests
        can run independently if surveyor's daemon module fails to
        load.
        """
        if not self.cfg.labeler.require_text_anchor:
            return list(proposed_tags)
        if not proposed_tags:
            return []
        from .cleanup import _build_record_corpus, _tag_anchored_in_corpus
        corpus = _build_record_corpus(record.frontmatter, record.body)
        return [t for t in proposed_tags if _tag_anchored_in_corpus(t, corpus)]

    def _entity_name_appears_in_record(
        self,
        entity_path: str,
        record: "VaultRecord",
    ) -> bool:
        """Word-boundary check for the entity's display name in the
        record's text surfaces.

        Returns ``True`` when:
          * config has ``require_text_anchor=False`` (legacy threshold-
            only contract; opt-out for tests + downstream workflows
            that explicitly want cosine-only semantic), OR
          * the entity's display name appears as a word-boundary
            match in the record's body / title / description /
            related list / relationships array's anchor strings

        Returns ``False`` when require_text_anchor is True and no
        textual presence is found — caller drops the candidate from
        the write list and emits ``surveyor.entity_link_blocked_no_text_anchor``.

        Reuses cleanup module helpers (``_display_name_from_path`` +
        ``_has_textual_presence`` + ``_build_record_corpus``) for
        byte-identical parity with the bulk-cleanup heuristic. Local
        import inside the method so the cleanup module's tests can
        run independently if surveyor's daemon module fails to load.
        """
        if not self.cfg.entity_link.require_text_anchor:
            return True
        from .cleanup import (
            _build_record_corpus,
            _display_name_from_path,
            _has_textual_presence,
        )
        display_name = _display_name_from_path(entity_path)
        corpus = _build_record_corpus(record.frontmatter, record.body)
        return _has_textual_presence(corpus, display_name)

    def _gate_entity_link_no_entities_observability(
        self,
        records: dict[str, "VaultRecord"],
    ) -> bool:
        """Phase 5 Sub-arc B once-per-lifecycle observability gate for
        the entity-link no-entities-in-vault state.

        Hypatia's vault has no ``matter``/``person``/``org``/``project``
        records, so all three entity-link helpers (cluster, noise,
        backfill) silently no-op every sweep. Per
        ``feedback_intentionally_left_blank.md``, silence-from-no-data
        must be distinguishable from silence-from-failure: emit a
        single ``surveyor.entity_link_no_entities_in_vault`` log when
        we first observe the empty-entities state, suppress on
        subsequent sweeps that stay empty.

        Gate semantics:
          * Returns ``True`` when entities are present → caller proceeds
            with stage 5/6/7. Resets the lifecycle latch so a later
            transition back to empty re-emits.
          * Returns ``False`` when entities are absent → caller skips
            stage 5/6/7. Emits the observability log iff the latch was
            previously False (i.e. first sweep in the empty state).

        Single emission point is at this gate (not per-cluster, not
        per-sibling helper) so the three downstream helpers
        (`_link_entities_in_clusters`, `_link_noise_points_to_entities`,
        `_backfill_new_entities`) share the same observability surface.
        """
        from .labeler import ENTITY_RECORD_TYPES
        has_entities = any(
            r.record_type in ENTITY_RECORD_TYPES
            for r in records.values()
        )
        if not has_entities:
            if not self._entity_link_no_entities_logged:
                log.info(
                    "surveyor.entity_link_no_entities_in_vault",
                    entity_types_searched=sorted(ENTITY_RECORD_TYPES),
                    vault_path=str(self.cfg.vault.path),
                )
                self._entity_link_no_entities_logged = True
            return False
        # Entities present — reset the latch so a transition back to
        # empty re-emits the log.
        self._entity_link_no_entities_logged = False
        return True

    def _link_entities_in_clusters(
        self,
        changed_cluster_ids: set[int],
        cluster_members: dict[int, list[str]],
        records: dict[str, "VaultRecord"],
        all_paths: list[str],
        all_vectors,
    ) -> None:
        """Write structured entity-link frontmatter for records whose cluster
        contains entity records (matter/person/org/project).

        For each non-entity member of a changed cluster, compute cosine
        similarity against each entity member. Above threshold → add the
        entity's vault path to the appropriate typed frontmatter field.

        Requires numpy; surveyor already imports it for embedding work.
        """
        import numpy as np
        from .labeler import ENTITY_RECORD_TYPES

        # Build path → vector lookup once (all_paths + all_vectors come from
        # embedder.get_all_embeddings()).
        path_to_vec: dict[str, "np.ndarray"] = {}
        for p, v in zip(all_paths, all_vectors):
            path_to_vec[p] = np.asarray(v, dtype=np.float32)

        # Entity type → frontmatter field name → writer method.
        # Kept local to avoid polluting module namespace.
        writer_methods = {
            "matter": self.writer.write_related_matters,
            "person": self.writer.write_related_persons,
            "org": self.writer.write_related_orgs,
            "project": self.writer.write_related_projects,
        }

        threshold = self.cfg.entity_link.threshold
        max_per = self.cfg.entity_link.max_per_record

        total_added = 0
        clusters_processed = 0
        for cid in changed_cluster_ids:
            members = cluster_members.get(cid, [])
            if len(members) < 2:
                continue

            # Partition: entities by type, regulars separately
            entities_by_type: dict[str, list[str]] = {}
            regulars: list[str] = []
            for path in members:
                record = records.get(path)
                if record is None:
                    continue
                if record.record_type in ENTITY_RECORD_TYPES:
                    entities_by_type.setdefault(record.record_type, []).append(path)
                else:
                    regulars.append(path)

            if not entities_by_type or not regulars:
                continue
            clusters_processed += 1

            for reg_path in regulars:
                reg_vec = path_to_vec.get(reg_path)
                if reg_vec is None:
                    continue
                reg_record = records.get(reg_path)
                reg_type = reg_record.record_type if reg_record else ""

                for entity_type, entity_paths in entities_by_type.items():
                    # Compute cos(reg_path, each entity), keep those above
                    # threshold, sort by similarity desc, cap at max_per.
                    scored: list[tuple[str, float]] = []
                    for e_path in entity_paths:
                        e_vec = path_to_vec.get(e_path)
                        if e_vec is None:
                            continue
                        # Vectors are already L2-normalised in the embedder,
                        # so dot product == cosine similarity.
                        sim = float(np.dot(reg_vec, e_vec))
                        if sim >= threshold:
                            scored.append((e_path, sim))

                    if not scored:
                        continue

                    scored.sort(key=lambda x: x[1], reverse=True)
                    capped = scored[:max_per]

                    # Per-write text-anchor gate: drop any candidate
                    # whose entity name has no textual presence in
                    # reg_record. Phase 1 source-side fix — closes
                    # the contamination class the cleanup CLI is
                    # currently repairing historical state for.
                    reg_record_obj = records.get(reg_path)
                    to_write: list[str] = []
                    sims_to_write: list[float] = []
                    for e_path, sim in capped:
                        if reg_record_obj is None or self._entity_name_appears_in_record(
                            e_path, reg_record_obj,
                        ):
                            to_write.append(e_path)
                            sims_to_write.append(sim)
                        else:
                            log.info(
                                "surveyor.entity_link_blocked_no_text_anchor",
                                record_path=reg_path,
                                entity_path=e_path,
                                similarity=round(sim, 4),
                                threshold=threshold,
                                stage="cluster",
                                cluster_id=cid,
                            )
                    if not to_write:
                        continue

                    method = writer_methods[entity_type]
                    # Attribution log: forensic trail per
                    # contamination diagnostic. Operator can grep
                    # ``writer.entity_links_written`` + filter on
                    # stage=cluster + cluster_id=N to reconstruct
                    # which cluster led to which link.
                    added = method(
                        reg_path, to_write, max_total=max_per,
                        attribution={
                            "stage": "cluster",
                            "cluster_id": cid,
                            "source_type": reg_type,
                            "similarities": sims_to_write,
                            "target_paths": to_write,
                        },
                    )
                    total_added += added

        if clusters_processed > 0:
            log.info(
                "daemon.entity_linking_complete",
                clusters_processed=clusters_processed,
                links_added=total_added,
                threshold=threshold,
                max_per_record=max_per,
            )

    def _link_noise_points_to_entities(
        self,
        noise_paths: list[str],
        records: dict[str, "VaultRecord"],
        all_paths: list[str],
        all_vectors,
    ) -> None:
        """Write related_* frontmatter for records that HDBSCAN assigned to
        noise (cluster id -1).

        These records aren't members of any cluster, so
        `_link_entities_in_clusters` never touches them — they'd stay
        unlinked forever. Instead, compare each noise point's embedding
        directly against every matter/person/org/project record in the
        vault and write the same typed frontmatter fields above threshold.

        Entity records that are themselves noise points are excluded as
        link *targets* (we don't want matter→matter self-links); they're
        still included as *sources* (their own related_* fields get
        populated if they sit alone in cluster-noise).
        """
        import numpy as np
        from .labeler import ENTITY_RECORD_TYPES

        path_to_vec: dict[str, "np.ndarray"] = {}
        for p, v in zip(all_paths, all_vectors):
            path_to_vec[p] = np.asarray(v, dtype=np.float32)

        # Collect entity paths by type from the FULL vault, not just cluster
        # members — we want to be able to link to an entity even when that
        # entity lives in a different cluster (or noise) from the source.
        all_entities_by_type: dict[str, list[str]] = {}
        for path, record in records.items():
            if record.record_type in ENTITY_RECORD_TYPES:
                all_entities_by_type.setdefault(record.record_type, []).append(path)

        if not all_entities_by_type:
            return

        writer_methods = {
            "matter": self.writer.write_related_matters,
            "person": self.writer.write_related_persons,
            "org": self.writer.write_related_orgs,
            "project": self.writer.write_related_projects,
        }

        threshold = self.cfg.entity_link.threshold
        max_per = self.cfg.entity_link.max_per_record

        total_added = 0
        noise_processed = 0
        for np_path in noise_paths:
            record = records.get(np_path)
            if record is None:
                continue
            np_vec = path_to_vec.get(np_path)
            if np_vec is None:
                continue

            # Determine this noise point's own record_type so we can skip
            # linking an entity to itself (and so matter→matter / person→
            # person self-links don't propagate through).
            src_type = record.record_type
            noise_processed += 1

            for entity_type, entity_paths in all_entities_by_type.items():
                scored: list[tuple[str, float]] = []
                for e_path in entity_paths:
                    if e_path == np_path:
                        continue  # self-link
                    # Also skip cross-type self-reference (e.g. linking a
                    # matter TO another matter via related_matters): the
                    # source was itself an entity of the same type, so that
                    # field shouldn't be used as a graph link for peers.
                    if src_type == entity_type:
                        continue
                    e_vec = path_to_vec.get(e_path)
                    if e_vec is None:
                        continue
                    sim = float(np.dot(np_vec, e_vec))
                    if sim >= threshold:
                        scored.append((e_path, sim))

                if not scored:
                    continue

                scored.sort(key=lambda x: x[1], reverse=True)
                capped = scored[:max_per]

                # Per-write text-anchor gate (Phase 1 source-side fix).
                # Noise-point linking has the second-widest blast radius
                # (every noise record vs every entity in the vault) so
                # the gate matters most here for precision.
                np_record_obj = records.get(np_path)
                to_write: list[str] = []
                sims_to_write: list[float] = []
                for e_path, sim in capped:
                    if np_record_obj is None or self._entity_name_appears_in_record(
                        e_path, np_record_obj,
                    ):
                        to_write.append(e_path)
                        sims_to_write.append(sim)
                    else:
                        log.info(
                            "surveyor.entity_link_blocked_no_text_anchor",
                            record_path=np_path,
                            entity_path=e_path,
                            similarity=round(sim, 4),
                            threshold=threshold,
                            stage="noise",
                        )
                if not to_write:
                    continue

                method = writer_methods[entity_type]
                # Attribution log: stage=noise so the forensic
                # trail can distinguish noise-point links from
                # cluster-member links — they have different
                # cardinality (noise compares against ALL vault
                # entities, not just cluster members) and so
                # different contamination risk profiles.
                added = method(
                    np_path, to_write, max_total=max_per,
                    attribution={
                        "stage": "noise",
                        "cluster_id": "noise",
                        "source_type": src_type,
                        "similarities": sims_to_write,
                        "target_paths": to_write,
                    },
                )
                total_added += added

        if noise_processed > 0:
            log.info(
                "daemon.noise_linking_complete",
                noise_processed=noise_processed,
                links_added=total_added,
                threshold=threshold,
                max_per_record=max_per,
            )

    def _backfill_new_entities(
        self,
        new_entity_paths: list[str],
        records: dict[str, "VaultRecord"],
        all_paths: list[str],
        all_vectors,
    ) -> None:
        """Reverse-direction scan for newly-created entity records.

        For each new entity E, walk every non-entity record R in the vault.
        If cos(E, R) >= threshold, append E's path to R's related_<E.type>
        frontmatter (up to max_per_record per field).

        This is the complement of the cluster/noise passes — they link FROM
        each record TO its nearest entities; this pass links FROM a new
        entity OUTWARDS to every record it's close to, so the new entity
        has a structural footprint immediately on creation.

        Cost: |new_entities| × |non_entity_records| numpy dot products.
        A new matter on David's ~3500-record vault = ~3500 dot products
        = sub-second.
        """
        import numpy as np
        from .labeler import ENTITY_RECORD_TYPES

        path_to_vec: dict[str, "np.ndarray"] = {}
        for p, v in zip(all_paths, all_vectors):
            path_to_vec[p] = np.asarray(v, dtype=np.float32)

        writer_methods = {
            "matter": self.writer.write_related_matters,
            "person": self.writer.write_related_persons,
            "org": self.writer.write_related_orgs,
            "project": self.writer.write_related_projects,
        }

        threshold = self.cfg.entity_link.threshold
        max_per = self.cfg.entity_link.max_per_record

        total_added = 0
        entities_processed = 0
        for entity_path in new_entity_paths:
            entity_record = records.get(entity_path)
            if entity_record is None:
                continue
            entity_type = entity_record.record_type
            if entity_type not in ENTITY_RECORD_TYPES:
                continue
            entity_vec = path_to_vec.get(entity_path)
            if entity_vec is None:
                continue

            entities_processed += 1
            method = writer_methods[entity_type]

            # For each non-entity record in the vault, check similarity.
            # Entity→entity self-type suppressed (same rule as noise
            # linking): a new matter doesn't get added to another matter's
            # related_matters list.
            for other_path, other_record in records.items():
                if other_path == entity_path:
                    continue
                if other_record.record_type == entity_type:
                    continue  # self-type suppression
                other_vec = path_to_vec.get(other_path)
                if other_vec is None:
                    continue
                sim = float(np.dot(entity_vec, other_vec))
                if sim < threshold:
                    continue
                # Per-write text-anchor gate (Phase 1 source-side fix).
                # Backfill has the WIDEST blast radius (one new entity
                # → potentially every record in the vault) so the gate
                # matters most here for precision. Pre-fix this stage
                # was the dominant contamination source — a new
                # ``person/X.md`` with topic-coherent embedding would
                # land in hundreds of records' related_persons even
                # when X is never mentioned.
                if not self._entity_name_appears_in_record(
                    entity_path, other_record,
                ):
                    log.info(
                        "surveyor.entity_link_blocked_no_text_anchor",
                        record_path=other_path,
                        entity_path=entity_path,
                        similarity=round(sim, 4),
                        threshold=threshold,
                        stage="backfill",
                    )
                    continue
                # Write ONE entity at a time so each target record's
                # max_per_record cap is respected independently.
                #
                # Attribution log: stage=backfill. This stage has the
                # widest blast radius (one new entity → potentially
                # every record in the vault) so log discipline matters
                # here most. Per
                # ``feedback_intentionally_left_blank.md``: silent
                # bulk linking is exactly the failure mode the QA
                # finding caught.
                added = method(
                    other_path, [entity_path], max_total=max_per,
                    attribution={
                        "stage": "backfill",
                        "cluster_id": "backfill",
                        "source_type": other_record.record_type,
                        "similarities": [sim],
                        "target_paths": [entity_path],
                    },
                )
                total_added += added

        if entities_processed > 0:
            log.info(
                "daemon.entity_backfill_complete",
                entities_processed=entities_processed,
                links_added=total_added,
                threshold=threshold,
                max_per_record=max_per,
            )

    # ---- Stage 8: cluster→MOC suggestion emission (Sub-arc D1) ----

    async def _maybe_emit_moc_suggestions(
        self,
        *,
        cluster_members: dict[int, list[str]],
        all_changed: set[int],
        records: dict[str, "VaultRecord"],
    ) -> int:
        """Walk labeled clusters, build candidate MOC suggestions, and
        persist to the out-of-vault JSONL queue.

        Returns the number of NEW proposals added (refreshes don't
        count). Always returns 0 when the stage is disabled or when
        no candidates are eligible — never raises to the caller.

        Per ratified design D1 (2026-05-19), this stage produces
        SUGGESTIONS only. The accept path (D2 slash commands) edits
        each member record's ``mocs:`` frontmatter via canonical
        ``vault_edit``, which triggers the Phase 4 Sub-arc A
        member-append hook. No vault writes here.

        Failure-isolated. A queue-write failure logs but doesn't
        propagate so a surveyor sweep is never broken by a queue
        I/O hiccup.
        """
        cfg = self.cfg.moc_suggestion
        if not cfg.enabled:
            # Per ``feedback_intentionally_left_blank.md``: silence-
            # from-disabled must surface ONCE so an operator
            # debugging "why are there no MOC suggestions?" can grep
            # for this log line. Lifecycle-gated to keep the noise
            # floor low.
            if not self._moc_suggestion_disabled_logged:
                log.info(
                    "surveyor.moc_suggestion.stage_disabled",
                    reason="moc_suggestion.enabled is False (default for Salem/KAL-LE; set true in config to enable)",
                )
                self._moc_suggestion_disabled_logged = True
            return 0
        # Re-arm the disabled-log latch in case the flag flips on
        # mid-process (future config-reload safety; harmless today).
        self._moc_suggestion_disabled_logged = False

        from .moc_suggester import (
            build_existing_mocs_index,
            propose_moc_suggestions,
        )
        from .moc_suggestion_queue import (
            derive_default_queue_path,
            upsert_proposals,
        )
        from pathlib import Path as _Path

        # Resolve queue path — config override OR derived from state.
        if cfg.queue_path:
            queue_path = _Path(cfg.queue_path)
        else:
            queue_path = derive_default_queue_path(self.cfg.state.path)

        # Build the MOC index ONCE per sweep (vault read).
        existing_mocs = build_existing_mocs_index(self.cfg.vault.path)

        all_proposals = []
        clusters_evaluated = 0
        for cid in all_changed:
            members = cluster_members.get(cid, [])
            if len(members) < cfg.min_cluster_size:
                continue
            # Pull the cluster's tags from the just-written cluster
            # state. ``label_cluster`` ran during this sweep, so the
            # state has the freshest tags. Fall back to empty tag
            # list when the cluster wasn't labeled (e.g., LLM call
            # failed) — propose_new can still trigger off member
            # paths even without tags (member-overlap signal is
            # independent of tags).
            cluster_key = f"semantic_{cid}"
            cluster_state = self.state.clusters.get(cluster_key)
            cluster_tags = list(cluster_state.label) if cluster_state else []

            clusters_evaluated += 1
            proposals = propose_moc_suggestions(
                cluster_id=cid,
                member_paths=members,
                cluster_tags=cluster_tags,
                records=records,
                existing_mocs=existing_mocs,
                member_overlap_threshold=cfg.member_overlap_threshold,
                fuzzy_label_jaccard_threshold=cfg.fuzzy_label_jaccard_threshold,
                min_cluster_size=cfg.min_cluster_size,
            )
            all_proposals.extend(proposals)

        if not all_proposals:
            # Empty-state per ``feedback_intentionally_left_blank.md``:
            # if the stage ran but produced no candidates, say so
            # explicitly. Distinguishes "ran, nothing eligible" from
            # "didn't run at all."
            log.info(
                "surveyor.moc_suggestion.no_candidates",
                clusters_evaluated=clusters_evaluated,
                existing_moc_count=len(existing_mocs),
                queue_path=str(queue_path),
            )
            return 0

        try:
            n_added, n_refreshed = upsert_proposals(
                queue_path,
                all_proposals,
                max_pending_per_target=cfg.max_pending_per_target,
                max_proposals_per_sweep=cfg.max_proposals_per_sweep,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "surveyor.moc_suggestion.upsert_failed",
                queue_path=str(queue_path),
                error=str(exc)[:200],
            )
            return 0

        log.info(
            "surveyor.moc_suggestion.sweep_summary",
            clusters_evaluated=clusters_evaluated,
            proposals_seen=len(all_proposals),
            added=n_added,
            refreshed=n_refreshed,
            queue_path=str(queue_path),
        )
        return n_added
