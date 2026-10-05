const CACHE_NAME = "valkiria-turnos-v1";
const ARCHIVOS_ESTATICOS = [
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ARCHIVOS_ESTATICOS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((nombres) =>
      Promise.all(
        nombres
          .filter((nombre) => nombre !== CACHE_NAME)
          .map((nombre) => caches.delete(nombre))
      )
    )
  );
  self.clients.claim();
});

// Estrategia: network-first para las páginas (siempre datos frescos de reservas/pagos),
// cache-first para los archivos estáticos (CSS, íconos). Si no hay red y no está en
// caché, muestra un aviso simple en vez de dejar la pantalla en blanco.
self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const esEstatico = ARCHIVOS_ESTATICOS.some((ruta) => request.url.endsWith(ruta));

  if (esEstatico) {
    event.respondWith(
      caches.match(request).then((cacheado) => cacheado || fetch(request))
    );
    return;
  }

  event.respondWith(
    fetch(request).catch(
      () =>
        new Response(
          "<html><body style='background:#0c0c0e;color:#fff;font-family:sans-serif;text-align:center;padding:40px;'>" +
            "<h2>Sin conexión</h2><p>Revisá tu internet e intentá de nuevo.</p></body></html>",
          { headers: { "Content-Type": "text/html" } }
        )
    )
  );
});
