import { build } from 'esbuild';
import { mkdir, copyFile, writeFile } from 'node:fs/promises';

await mkdir('dist', {recursive:true});
await build({
  entryPoints:['src/app.js'],
  outdir:'dist',
  entryNames:'app',
  chunkNames:'chunks/[name]-[hash]',
  splitting:true,
  bundle:true,
  minify:true,
  format:'esm',
  target:['es2022'],
  legalComments:'none',
  sourcemap:false
});
await copyFile('public/index.html','dist/index.html');
// PDF.js worker must exactly match the bundled library version.
await copyFile('node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs',
  'dist/pdf.worker.min.mjs');
await copyFile('public/styles.css','dist/styles.css');
await writeFile('dist/_headers', [
  '/*',
  '  X-Content-Type-Options: nosniff',
  '  Referrer-Policy: no-referrer',
  '  X-Frame-Options: DENY',
  "  Content-Security-Policy: default-src 'self'; connect-src 'none'; script-src 'self'; worker-src 'self' blob:; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
  '  Permissions-Policy: camera=(), microphone=(), geolocation=()',
  ''
].join('\n'));
console.log('Static Cloudflare Pages demo built → demo/dist/');
