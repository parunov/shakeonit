"use strict";

// Telegram keeps its native loading placeholder visible until it receives this event.
// Signal readiness from the already rendered shell without waiting for the full remote SDK.
(() => {
  try {
    if (window.Telegram?.WebApp?.ready) {
      window.Telegram.WebApp.ready();
      return;
    }

    const eventType = "web_app_ready";
    const eventData = "";
    if (window.TelegramWebviewProxy?.postEvent) {
      window.TelegramWebviewProxy.postEvent(eventType, JSON.stringify(eventData));
    } else if (window.external && "notify" in window.external) {
      window.external.notify(JSON.stringify({ eventType, eventData }));
    } else if (window.parent !== window) {
      window.parent.postMessage(JSON.stringify({ eventType, eventData }), "*");
    }
  } catch (error) {
    // The full Telegram SDK will retry WebApp.ready() when it becomes available.
  }
})();
