// Minimal service worker. Its only job is to make the app installable to the
// home screen. It deliberately does NOT cache state.json - stale prices in
// a trading tracker would be worse than no prices.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
