// Injected first into every proxied HTML page (see proxy.py). Plain ES2017, no
// dependencies. It must not contain the closing tag of a script element.
//
// 1. Keeps URLs a page builds at runtime inside the proxy: "/api/x",
//    location.host + "/ws" and absolute links to the device itself would
//    otherwise escape to Home Assistant's own paths.
// 2. Keeps site cookies server side (both modes): document.cookie is backed by
//    the per-(user, view) cookie jar the proxy keeps, never by the browser's jar
//    for Home Assistant's origin.
// 3. Isolated views only: gives the page localStorage and sessionStorage, which
//    an opaque origin lacks (they throw), backed by server-side storage.
function (cfg) {
  "use strict";
  var loc = window.location;
  // location.origin is "null" in an isolated view; the URL's own origin is what
  // relative URLs resolve against
  var here = loc.protocol + "//" + loc.host;
  var prefix = cfg.prefix; // /api/local_web_ui/<view_id>/<token>
  var viewPath = cfg.viewPath; // /api/local_web_ui/<view_id>/
  var site = cfg.site; // the device's own origin, e.g. http://192.168.1.50

  function rewrite(input) {
    if (input === null || input === undefined) return input;
    var text = String(input);
    var url;
    try {
      url = new URL(text, document.baseURI);
    } catch (e) {
      return input;
    }
    var proto = url.protocol;
    var ws = proto === "ws:" || proto === "wss:";
    if (!ws && proto !== "http:" && proto !== "https:") return input;
    var origin = (ws ? (proto === "wss:" ? "https:" : "http:") : proto) + "//" + url.host;
    if (origin === here) {
      if (url.pathname.indexOf(viewPath) === 0) return input; // Already proxied
    } else if (origin !== site) {
      return input; // Another site entirely
    }
    var out = new URL(prefix + url.pathname + url.search + url.hash, here);
    if (ws) out.protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    return out.href;
  }

  // The page URL holds the session token: a page may not send it to other sites
  function noReferrer(init) {
    return init && init.referrerPolicy ? Object.assign({}, init, { referrerPolicy: "no-referrer" }) : init;
  }

  try {
    var nativeFetch = window.fetch;
    if (nativeFetch) {
      window.fetch = function (input, init) {
        if (typeof input === "string" || input instanceof URL) {
          input = rewrite(input);
        } else if (input instanceof Request) {
          var moved = rewrite(input.url);
          if (moved !== input.url) {
            // A Request's URL cannot change: copy it, body included
            var req = input;
            var self = this;
            var noBody = req.method === "GET" || req.method === "HEAD";
            return (noBody ? Promise.resolve(undefined) : req.arrayBuffer()).then(function (body) {
              var copy = new Request(moved, {
                method: req.method,
                headers: req.headers,
                body: body,
                mode: req.mode === "navigate" ? "same-origin" : req.mode,
                credentials: req.credentials,
                cache: req.cache,
                redirect: req.redirect,
                referrerPolicy: "no-referrer",
                integrity: req.integrity,
                keepalive: req.keepalive,
                signal: req.signal,
              });
              return nativeFetch.call(self, copy, noReferrer(init));
            });
          }
          if (input.referrerPolicy) input = new Request(input, { referrerPolicy: "no-referrer" });
        }
        return nativeFetch.call(this, input, noReferrer(init));
      };
    }
    var open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var args = Array.prototype.slice.call(arguments);
      args[1] = rewrite(args[1]);
      return open.apply(this, args);
    };
    if (window.EventSource) {
      var NativeEventSource = window.EventSource;
      window.EventSource = class extends NativeEventSource {
        constructor(url, options) {
          super(rewrite(url), options);
        }
      };
    }
    if (window.WebSocket) {
      var NativeWebSocket = window.WebSocket;
      window.WebSocket = class extends NativeWebSocket {
        constructor(url, protocols) {
          if (protocols === undefined) super(rewrite(url));
          else super(rewrite(url), protocols);
        }
      };
    }
    ["pushState", "replaceState"].forEach(function (name) {
      var original = history[name];
      history[name] = function (state, title, url) {
        if (url === undefined || url === null) return original.call(this, state, title);
        return original.call(this, state, title, rewrite(url));
      };
    });
    var nativeOpen = window.open;
    window.open = function () {
      var args = Array.prototype.slice.call(arguments);
      if (args[0]) args[0] = rewrite(args[0]);
      return nativeOpen.apply(this, args);
    };
    var setAttribute = Element.prototype.setAttribute;
    var linkAttrs = { src: 1, href: 1, action: 1, formaction: 1, poster: 1 };
    Element.prototype.setAttribute = function (name, value) {
      var lower = String(name).toLowerCase();
      if (lower === "referrerpolicy") return undefined; // See noReferrer
      if (typeof value === "string" && linkAttrs[lower]) value = rewrite(value);
      return setAttribute.call(this, name, value);
    };
    ["HTMLAnchorElement", "HTMLAreaElement", "HTMLImageElement", "HTMLIFrameElement",
     "HTMLLinkElement", "HTMLScriptElement"].forEach(function (name) {
      var Cls = window[name];
      var desc = Cls && Object.getOwnPropertyDescriptor(Cls.prototype, "referrerPolicy");
      if (!desc || !desc.set) return;
      Object.defineProperty(Cls.prototype, "referrerPolicy", {
        configurable: true,
        enumerable: desc.enumerable,
        get: desc.get,
        set: function () {},
      });
    });
    [
      ["HTMLImageElement", "src"],
      ["HTMLScriptElement", "src"],
      ["HTMLLinkElement", "href"],
      ["HTMLIFrameElement", "src"],
      ["HTMLAnchorElement", "href"],
      ["HTMLSourceElement", "src"],
      ["HTMLMediaElement", "src"],
      ["HTMLFormElement", "action"],
    ].forEach(function (pair) {
      var Cls = window[pair[0]];
      if (!Cls) return;
      var desc = Object.getOwnPropertyDescriptor(Cls.prototype, pair[1]);
      if (!desc || !desc.set) return;
      Object.defineProperty(Cls.prototype, pair[1], {
        configurable: true,
        enumerable: desc.enumerable,
        get: desc.get,
        set: function (value) {
          desc.set.call(this, rewrite(value));
        },
      });
    });
    // Links and forms whose markup the server-side rewrite did not see
    document.addEventListener(
      "click",
      function (event) {
        var link = event.target && event.target.closest && event.target.closest("a[href]");
        if (link) link.setAttribute("href", link.getAttribute("href"));
      },
      true
    );
    document.addEventListener(
      "submit",
      function (event) {
        var form = event.target;
        if (form && form.getAttribute && form.getAttribute("action")) {
          form.setAttribute("action", form.getAttribute("action"));
        }
      },
      true
    );
    // A service worker registered from Home Assistant's origin would outlive the
    // view and could intercept Home Assistant itself
    if (navigator.serviceWorker) {
      navigator.serviceWorker.register = function () {
        return Promise.reject(new DOMException("Service workers are not available here", "SecurityError"));
      };
    }
  } catch (e) {
    /* A page that breaks one of these still gets the others */
  }

  var own = Object.prototype.hasOwnProperty;
  var nativePost = nativeFetch || window.fetch;
  function post(name, body) {
    try {
      return nativePost.call(window, prefix + "/__lwu/" + name, {
        method: "POST",
        body: body,
        // keepalive lets a write outlive the page, within a 64 KiB budget
        keepalive: body.length < 60000,
        // A CORS-safelisted type: no preflight
        headers: { "Content-Type": "text/plain" },
      });
    } catch (e) {
      return null; // Best effort
    }
  }

  // Site cookies are kept server side in both modes: device cookies must not land
  // in (or read) Home Assistant's own cookie jar. Scripts see the non-HttpOnly ones.
  var cookies = cfg.cookies || {};
  try {
    Object.defineProperty(Document.prototype, "cookie", {
      configurable: true,
      get: function () {
        return Object.keys(cookies)
          .map(function (key) {
            return key + "=" + cookies[key];
          })
          .join("; ");
      },
      set: function (value) {
        value = String(value);
        var pair = value.split(";")[0];
        var eq = pair.indexOf("=");
        if (eq < 1) return;
        var key = pair.slice(0, eq).trim();
        if (/;\s*(max-age=(0|-)|expires=thu, 01 jan 1970)/i.test(value)) delete cookies[key];
        else cookies[key] = pair.slice(eq + 1).trim();
        post("cookie", value);
      },
    });
  } catch (e) {
    /* Not overridable in this browser */
  }

  if (!cfg.isolated) return;
  try {
    window.localStorage.length;
    return; // Storage works natively (not actually sandboxed)
  } catch (e) {
    /* Opaque origin: emulate below */
  }

  // These throw in an opaque origin; without them pages fall back gracefully
  // ("indexedDB" in window, "serviceWorker" in navigator, ...)
  try {
    delete Navigator.prototype.serviceWorker;
    delete window.caches;
    delete window.indexedDB;
  } catch (e) {
    /* Keep them */
  }

  // localStorage is written back as changes, each with an id. The last write of
  // a page can reach the server after the next page was already rendered (save,
  // then reload), so the page remembers that id in window.name, which survives
  // navigation, and the next page fetches fresh data if its copy predates it.
  var MARK = "\u0001lwu:";
  var stored = cfg.storage || {};
  var name = window.name;
  if (name.indexOf(MARK) === 0 && name.indexOf("|") > 0) {
    var marker = name.slice(MARK.length, name.indexOf("|")).split(":");
    name = name.slice(name.indexOf("|") + 1);
    try {
      window.name = name;
    } catch (e) {
      /* Read-only in this context */
    }
    var writeId = marker[0];
    var recent = Date.now() - Number(marker[1]) < 60000;
    if (writeId && recent && (cfg.writes || []).indexOf(writeId) < 0) {
      try {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", prefix + "/__lwu/storage?w=" + encodeURIComponent(writeId), false);
        xhr.send();
        if (xhr.status === 200) stored = JSON.parse(xhr.responseText);
      } catch (e) {
        /* Keep the copy we have */
      }
    }
  }
  var lastWrite = null;

  function makeStorage(data, persist) {
    var changes = {};
    var cleared = false;
    var dirty = false;
    var timer = null;
    function flush() {
      clearTimeout(timer);
      timer = null;
      if (!dirty) return;
      var id = Math.random().toString(36).slice(2, 12);
      var body = JSON.stringify({ w: id, set: changes, clear: cleared });
      changes = {};
      cleared = false;
      dirty = false;
      lastWrite = [id, Date.now()];
      post("storage", body);
    }
    function changed(key, value) {
      if (!persist) return;
      if (key === null) {
        changes = {};
        cleared = true;
      } else {
        changes[key] = value;
      }
      dirty = true;
      if (value && value.length > 16384) {
        flush(); // Too large to leave for the page's last moments
      } else if (!timer) {
        timer = setTimeout(flush, 250);
      }
    }
    if (persist) {
      window.addEventListener("pagehide", function () {
        flush();
        if (!lastWrite) return;
        try {
          window.name = MARK + lastWrite[0] + ":" + lastWrite[1] + "|" + name;
        } catch (e) {
          /* Read-only in this context */
        }
      });
    }
    var api = {
      getItem: function (key) {
        key = String(key);
        return own.call(data, key) ? data[key] : null;
      },
      setItem: function (key, value) {
        key = String(key);
        value = String(value);
        data[key] = value;
        changed(key, value);
      },
      removeItem: function (key) {
        key = String(key);
        if (!own.call(data, key)) return;
        delete data[key];
        changed(key, null);
      },
      clear: function () {
        Object.keys(data).forEach(function (key) {
          delete data[key];
        });
        changed(null, null);
      },
      key: function (index) {
        var keys = Object.keys(data);
        return index < keys.length ? keys[index] : null;
      },
    };
    Object.defineProperty(api, "length", {
      get: function () {
        return Object.keys(data).length;
      },
    });
    return new Proxy(api, {
      get: function (target, prop) {
        if (prop in target) return target[prop];
        return typeof prop === "string" && own.call(data, prop) ? data[prop] : undefined;
      },
      set: function (target, prop, value) {
        if (prop in target) return false;
        api.setItem(prop, value);
        return true;
      },
      deleteProperty: function (target, prop) {
        api.removeItem(prop);
        return true;
      },
      has: function (target, prop) {
        return prop in target || own.call(data, prop);
      },
      ownKeys: function () {
        return Object.keys(data);
      },
      getOwnPropertyDescriptor: function (target, prop) {
        if (own.call(data, prop)) {
          return { value: data[prop], writable: true, enumerable: true, configurable: true };
        }
        return undefined;
      },
    });
  }
  var local = makeStorage(stored, true);
  // Per page: it does not survive a reload, unlike a browser's
  var session = makeStorage({}, false);
  try {
    Object.defineProperty(window, "localStorage", { configurable: true, get: function () { return local; } });
    Object.defineProperty(window, "sessionStorage", { configurable: true, get: function () { return session; } });
  } catch (e) {
    /* Not overridable in this browser */
  }
}
