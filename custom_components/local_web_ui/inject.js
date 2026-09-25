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

  try {
    var nativeFetch = window.fetch;
    if (nativeFetch) {
      window.fetch = function (input, init) {
        if (typeof input === "string" || input instanceof URL) {
          input = rewrite(input);
        } else if (input && input.url) {
          var moved = rewrite(input.url);
          if (moved !== input.url) input = new Request(moved, input);
        }
        return nativeFetch.call(this, input, init);
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
      if (typeof value === "string" && linkAttrs[String(name).toLowerCase()]) value = rewrite(value);
      return setAttribute.call(this, name, value);
    };
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
      nativePost.call(window, prefix + "/__lwu/" + name, {
        method: "POST",
        body: body,
        keepalive: true,
        headers: { "Content-Type": "text/plain" },
      });
    } catch (e) {
      /* Best effort */
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
  function makeStorage(data, persist) {
    var timer = null;
    function flush() {
      timer = null;
      post("storage", JSON.stringify(data));
    }
    function changed() {
      if (!persist) return;
      clearTimeout(timer);
      timer = setTimeout(flush, 250);
    }
    if (persist) {
      window.addEventListener("pagehide", function () {
        if (timer) {
          clearTimeout(timer);
          flush();
        }
      });
    }
    var api = {
      getItem: function (key) {
        key = String(key);
        return own.call(data, key) ? data[key] : null;
      },
      setItem: function (key, value) {
        data[String(key)] = String(value);
        changed();
      },
      removeItem: function (key) {
        delete data[String(key)];
        changed();
      },
      clear: function () {
        Object.keys(data).forEach(function (key) {
          delete data[key];
        });
        changed();
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
  var local = makeStorage(cfg.storage || {}, true);
  var session = makeStorage({}, false);
  try {
    Object.defineProperty(window, "localStorage", { configurable: true, get: function () { return local; } });
    Object.defineProperty(window, "sessionStorage", { configurable: true, get: function () { return session; } });
  } catch (e) {
    /* Not overridable in this browser */
  }
}
