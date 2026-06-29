// Rasterize the single source SVG (public/icon.svg — the "Flowers for Algernon"
// daisy bloom, designed maskable-safe: full-bleed brand-green, bloom within the
// centre 80%) into every PNG size the PWA + favicons need. Re-run this whenever
// public/icon.svg changes:  `node scripts/gen-icons.mjs`
//
// One source, every size — do NOT hand-edit the generated PNGs. sharp renders the
// SVG via librsvg at a high density once per size, so each raster is crisp (vector
// supersampled down, never an upscaled bitmap).
import sharp from 'sharp';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const PUBLIC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'public');
const SOURCE = join(PUBLIC_DIR, 'icon.svg');

// Render density: the source viewBox is 512²; 384 DPI renders it at ~2730px so
// every downsample (incl. the 512 itself) is supersampled, not upscaled.
const DENSITY = 384;

// purpose:"any" 192 + 512, purpose:"maskable" 512 (same source — it's already
// maskable-safe), Apple touch 180, and favicon raster sizes.
const PNG_TARGETS = [
  { file: 'icon-192.png', size: 192 },
  { file: 'icon-512.png', size: 512 },
  { file: 'icon-maskable-512.png', size: 512 },
  { file: 'apple-touch-icon.png', size: 180 },
  { file: 'favicon-32.png', size: 32 },
  { file: 'favicon-16.png', size: 16 },
];

// favicon.ico bundles these sizes so the browser's automatic /favicon.ico request
// (and legacy clients) get a crisp icon without a 404.
const ICO_SIZES = [16, 32, 48];

async function renderPng(svg, size) {
  return sharp(svg, { density: DENSITY })
    .resize(size, size, { fit: 'cover' })
    .png({ compressionLevel: 9 })
    .toBuffer();
}

// Assemble a multi-image .ico from PNG-encoded entries (modern ICO permits PNG
// payloads). Header (6B) + one 16B directory entry per image + the PNG bytes.
function buildIco(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0); // reserved
  header.writeUInt16LE(1, 2); // type 1 = icon
  header.writeUInt16LE(images.length, 4); // image count

  const entries = [];
  const payloads = [];
  let offset = 6 + images.length * 16;
  for (const { size, buffer } of images) {
    const entry = Buffer.alloc(16);
    entry.writeUInt8(size >= 256 ? 0 : size, 0); // width (0 = 256)
    entry.writeUInt8(size >= 256 ? 0 : size, 1); // height (0 = 256)
    entry.writeUInt8(0, 2); // palette color count (0 = no palette)
    entry.writeUInt8(0, 3); // reserved
    entry.writeUInt16LE(1, 4); // color planes
    entry.writeUInt16LE(32, 6); // bits per pixel
    entry.writeUInt32LE(buffer.length, 8); // image data size
    entry.writeUInt32LE(offset, 12); // image data offset
    offset += buffer.length;
    entries.push(entry);
    payloads.push(buffer);
  }
  return Buffer.concat([header, ...entries, ...payloads]);
}

async function main() {
  const svg = await readFile(SOURCE);

  for (const { file, size } of PNG_TARGETS) {
    const buf = await renderPng(svg, size);
    await writeFile(join(PUBLIC_DIR, file), buf);
    console.log(`  wrote ${file} (${size}x${size}, ${buf.length} bytes)`);
  }

  const icoImages = [];
  for (const size of ICO_SIZES) {
    icoImages.push({ size, buffer: await renderPng(svg, size) });
  }
  const ico = buildIco(icoImages);
  await writeFile(join(PUBLIC_DIR, 'favicon.ico'), ico);
  console.log(`  wrote favicon.ico (${ICO_SIZES.join('/')}, ${ico.length} bytes)`);

  console.log('icons generated from public/icon.svg');
}

main().catch((err) => {
  console.error('icon generation failed:', err);
  process.exit(1);
});
