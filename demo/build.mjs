import { build } from 'esbuild';
import { mkdir, copyFile, writeFile } from 'node:fs/promises';

await mkdir('dist', {recursive:true});
await build({
  entryPoints:['src/app.js'],
  outfile:'dist/app.js',
  bundle:true,
  minify:true,
  format:'esm',
  target:['es2020'],
  legalComments:'none',
  sourcemap:false
});
await copyFile('public/index.html','dist/index.html');
await copyFile('public/styles.css','dist/styles.css');
await writeFile('dist/_headers', [
  '/*',
  '  X-Content-Type-Options: nosniff',
  '  Referrer-Policy: no-referrer',
  '  X-Frame-Options: DENY',
  "  Content-Security-Policy: default-src 'self'; connect-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
  '  Permissions-Policy: camera=(), microphone=(), geolocation=()',
  ''
].join('\n'));
console.log('Static Cloudflare Pages demo built → demo/dist/');
