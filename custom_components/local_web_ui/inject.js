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
// 4. Isolated views only: gives the page an in-memory IndexedDB (proxy.py loads
//    memory-indexeddb.js just before this script), and starts its web workers
//    through a loader, since an opaque origin cannot start them from the proxy's
//    URLs (see workerMain).
function (cfg) {
  "use strict";
  var memoryIndexedDB = window.__lwuMemoryIndexedDB;
  try {
    delete window.__lwuMemoryIndexedDB;
  } catch (e) {
    /* Not loaded */
  }
  var loc = window.location;
  // location.origin is "null" in an isolated view; the URL's own origin is what
  // relative URLs resolve against
  var here = loc.protocol + "//" + loc.host;
  var prefix = cfg.prefix; // /api/local_web_ui/<view_id>/<token>
  var viewPath = cfg.viewPath; // /api/local_web_ui/<view_id>/
  var site = cfg.site; // the device's own origin, e.g. http://192.168.1.50

  // Also copied into web workers as source text (see workerMain), so it uses
  // nothing from the enclosing function
  function makeRewrite(pageProtocol, pageHost, prefix, viewPath, site, baseURI) {
    var here = pageProtocol + "//" + pageHost;
    return function rewrite(input) {
      if (input === null || input === undefined) return input;
      var text = String(input);
      var url;
      try {
        url = new URL(text, baseURI());
      } catch (e) {
        return input;
      }
      var proto = url.protocol;
      var ws = proto === "ws:" || proto === "wss:";
      if (!ws && proto !== "http:" && proto !== "https:") return input;
      var origin = (ws ? (proto === "wss:" ? "https:" : "http:") : proto) + "//" + url.host;
      // The page's own scheme: "ws://" + location.host would be mixed content on https
      var scheme = ws ? (pageProtocol === "https:" ? "wss:" : "ws:") : pageProtocol;
      if (url.host === pageHost) {
        if (url.pathname.indexOf(viewPath) === 0) {
          if (proto === scheme) return input; // Already proxied
          url.protocol = scheme;
          return url.href;
        }
      } else if (origin !== site) {
        return input; // Another site entirely
      }
      var out = new URL(prefix + url.pathname + url.search + url.hash, here);
      out.protocol = scheme;
      return out.href;
    };
  }
  var rewrite = makeRewrite(loc.protocol, loc.host, prefix, viewPath, site, function () {
    return document.baseURI;
  });

  function unprefix(val) {
    if (typeof val !== "string") return val;
    if (val.indexOf(prefix) === 0) {
      var rest = val.slice(prefix.length);
      return rest.charAt(0) === "/" ? rest : "/" + rest;
    }
    var full = here + prefix;
    if (val.indexOf(full) === 0) {
      var rest = val.slice(full.length);
      return rest.charAt(0) === "/" ? rest : "/" + rest;
    }
    return val;
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
    // A Proxy rather than a subclass: instances keep the native prototype, which
    // pages check (noVNC refuses a socket whose prototype lacks its own "send")
    function rewriteConstructor(Native) {
      return new Proxy(Native, {
        construct: function (target, args, newTarget) {
          if (args.length) args[0] = rewrite(args[0]);
          return Reflect.construct(target, args, newTarget);
        },
      });
    }
    if (window.EventSource) window.EventSource = rewriteConstructor(window.EventSource);
    if (window.WebSocket) window.WebSocket = rewriteConstructor(window.WebSocket);
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
    var setAttributeNS = Element.prototype.setAttributeNS;
    var getAttribute = Element.prototype.getAttribute;
    var getAttributeNS = Element.prototype.getAttributeNS;
    var linkAttrs = {
      src: 1,
      href: 1,
      action: 1,
      formaction: 1,
      poster: 1,
      data: 1,
      background: 1,
    };
    Element.prototype.setAttribute = function (name, value) {
      var lower = String(name).toLowerCase();
      if (lower === "referrerpolicy") return undefined; // See noReferrer
      if (typeof value === "string" && linkAttrs[lower]) value = rewrite(value);
      return setAttribute.call(this, name, value);
    };
    if (setAttributeNS) {
      Element.prototype.setAttributeNS = function (ns, name, value) {
        var lower = String(name).toLowerCase();
        if (lower === "referrerpolicy") return undefined;
        if (typeof value === "string" && linkAttrs[lower]) value = rewrite(value);
        return setAttributeNS.call(this, ns, name, value);
      };
    }
    Element.prototype.getAttribute = function (name) {
      var val = getAttribute.call(this, name);
      if (typeof val === "string" && linkAttrs[String(name).toLowerCase()]) {
        return unprefix(val);
      }
      return val;
    };
    if (getAttributeNS) {
      Element.prototype.getAttributeNS = function (ns, name) {
        var val = getAttributeNS.call(this, ns, name);
        if (typeof val === "string" && linkAttrs[String(name).toLowerCase()]) {
          return unprefix(val);
        }
        return val;
      };
    }
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
    ["HTMLAnchorElement", "HTMLAreaElement"].forEach(function (name) {
      var Cls = window[name];
      if (!Cls) return;
      var desc = Object.getOwnPropertyDescriptor(Cls.prototype, "pathname");
      if (!desc || !desc.get || !desc.set) return;
      Object.defineProperty(Cls.prototype, "pathname", {
        configurable: true,
        enumerable: desc.enumerable,
        get: function () {
          return unprefix(desc.get.call(this));
        },
        set: function (value) {
          if (typeof value === "string") {
            var current = desc.get.call(this);
            if (current && (current.indexOf(prefix) === 0 || (this.origin === here && current.indexOf(viewPath) === 0))) {
              var path = value.charAt(0) === "/" ? value : "/" + value;
              desc.set.call(this, prefix + path);
              return;
            }
          }
          desc.set.call(this, value);
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
      nativePost
        .call(window, prefix + "/__lwu/" + name, {
          method: "POST",
          body: body,
          keepalive: body.length < 4096, // Outlives the page (cookies are small)
          headers: { "Content-Type": "text/plain" },
        })
        .catch(function () {
          /* Best effort */
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
            return key ? key + "=" + cookies[key] : cookies[key];
          })
          .join("; ");
      },
      set: function (value) {
        value = String(value);
        var pair = value.split(";")[0];
        var eq = pair.indexOf("=");
        // "flag" alone is a cookie without a name, as in browsers
        var key = eq < 0 ? "" : pair.slice(0, eq).trim();
        var maxAge = /;\s*max-age\s*=\s*(-?\d+)/i.exec(value);
        var expires = /;\s*expires\s*=\s*([^;]*)/i.exec(value);
        if (maxAge ? Number(maxAge[1]) <= 0 : expires && Date.parse(expires[1]) <= Date.now()) {
          delete cookies[key];
        } else {
          cookies[key] = pair.slice(eq + 1).trim();
        }
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
  // ("serviceWorker" in navigator, "caches" in window, ...)
  try {
    delete Navigator.prototype.serviceWorker;
    delete window.caches;
  } catch (e) {
    /* Keep them */
  }
  // IndexedDB in memory, for the current page: some apps (SilverBullet) cannot run
  // without one. The native classes are replaced too, so that instanceof checks
  // (the idb library's) recognise its objects.
  try {
    if (memoryIndexedDB) {
      Object.keys(memoryIndexedDB).forEach(function (name) {
        Object.defineProperty(window, name, {
          configurable: true,
          writable: true,
          value: memoryIndexedDB[name],
        });
      });
    } else {
      delete window.indexedDB;
    }
  } catch (e) {
    /* Keep it */
  }

  // Web workers. An opaque origin can start a worker from a blob: URL, but not
  // from the proxy's URLs (another origin to it), and not as a module worker at
  // all. So every worker of a proxied script starts as a classic worker from a
  // small blob: script, workerMain, which loads the real one.
  //
  // workerMain runs inside the worker. It keeps the worker's own requests in the
  // proxy, as this script does for the page, and shows the real script's URL as
  // location (relative URLs resolve against it). A module is loaded with import(),
  // which a classic worker may use; the messages that arrive meanwhile are held
  // and handed to the module's listeners once it has run. A classic script is
  // loaded with a synchronous XHR and eval, as importScripts would (which fails
  // for the proxy's URLs too, so it is replaced the same way).
  function workerMain(c, makeRewrite) {
    var rewrite = makeRewrite(c.protocol, c.host, c.prefix, c.viewPath, c.site, function () {
      return c.url;
    });
    var nativeFetch = self.fetch;
    self.fetch = function (input, init) {
      if (typeof input === "string" || input instanceof URL) {
        input = rewrite(input);
      } else if (input instanceof Request) {
        var moved = rewrite(input.url);
        if (moved !== input.url) {
          var req = input;
          var self_ = this;
          var noBody = req.method === "GET" || req.method === "HEAD";
          return (noBody ? Promise.resolve(undefined) : req.arrayBuffer()).then(function (body) {
            return nativeFetch.call(self_, new Request(moved, {
              method: req.method,
              headers: req.headers,
              body: body,
              mode: req.mode,
              credentials: req.credentials,
              cache: req.cache,
              redirect: req.redirect,
              integrity: req.integrity,
              signal: req.signal,
            }), init);
          });
        }
      }
      return nativeFetch.call(this, input, init);
    };
    var open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var args = Array.prototype.slice.call(arguments);
      args[1] = rewrite(args[1]);
      return open.apply(this, args);
    };
    ["EventSource", "WebSocket"].forEach(function (name) {
      if (!self[name]) return;
      self[name] = new Proxy(self[name], {
        construct: function (target, args, newTarget) {
          if (args.length) args[0] = rewrite(args[0]);
          return Reflect.construct(target, args, newTarget);
        },
      });
    });
    var real = new URL(c.url);
    var location = { origin: "null" }; // As the page's
    ["href", "protocol", "host", "hostname", "port", "pathname", "search", "hash"].forEach(function (key) {
      location[key] = real[key];
    });
    location.toString = function () {
      return real.href;
    };
    try {
      Object.defineProperty(self, "location", { configurable: true, get: function () { return location; } });
    } catch (e) {
      /* Keep the blob's */
    }
    var globalEval = eval;
    self.importScripts = function () {
      for (var i = 0; i < arguments.length; i++) {
        var url = rewrite(String(arguments[i]));
        var xhr = new XMLHttpRequest();
        xhr.open("GET", url, false);
        xhr.send();
        if (xhr.status < 200 || xhr.status >= 300) {
          throw new DOMException("Failed to load " + url, "NetworkError");
        }
        globalEval(xhr.responseText + "\n//# sourceURL=" + url);
      }
    };
    if (!c.module) {
      self.importScripts(c.url);
      return;
    }
    var held = [];
    function hold(event) {
      event.stopImmediatePropagation();
      held.push(event);
    }
    self.addEventListener("message", hold);
    // Through eval: this file stays ES2017, which has no import()
    globalEval("import(" + JSON.stringify(c.url) + ")").then(
      function () {
        self.removeEventListener("message", hold);
        held.forEach(function (event) {
          self.dispatchEvent(new MessageEvent("message", { data: event.data, ports: event.ports }));
        });
      },
      function (error) {
        // An error event on the page's Worker object, as a failed module load gives
        setTimeout(function () {
          throw error;
        });
      }
    );
  }
  try {
    var NativeWorker = window.Worker;
    if (NativeWorker) {
      window.Worker = new Proxy(NativeWorker, {
        construct: function (target, args, newTarget) {
          var options = args[1] || {};
          var module = options.type === "module";
          var url = rewrite(args[0]);
          var proto;
          try {
            url = new URL(String(url), document.baseURI).href;
            proto = new URL(url).protocol;
          } catch (e) {
            return Reflect.construct(target, args, newTarget);
          }
          // A classic worker from the page's own blob: or data: URL starts natively
          if (!module && proto !== "http:" && proto !== "https:") {
            return Reflect.construct(target, args, newTarget);
          }
          var conf = {
            url: url,
            module: module,
            protocol: loc.protocol,
            host: loc.host,
            prefix: prefix,
            viewPath: viewPath,
            site: site,
          };
          var source = "(" + workerMain + ")(" + JSON.stringify(conf) + ", " + makeRewrite + ");";
          var classic = Object.assign({}, options, { type: "classic" });
          var blob = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
          var worker = Reflect.construct(target, [blob, classic], newTarget);
          setTimeout(function () {
            URL.revokeObjectURL(blob);
          }, 60000);
          return worker;
        },
      });
    }
  } catch (e) {
    /* Keep the native one */
  }

  // localStorage is written back as changes. Each write has an id, and a page
  // number and sequence number that let the server drop a write that arrives
  // after a later one of the same page.
  //
  // The last write of a page can reach the server after the next page was
  // already rendered (save, then reload), so the page remembers that write's id
  // in window.name, which survives navigation, and the next page of the same view
  // fetches fresh data if its copy predates it.
  var MARK = "\u0001lwu:";
  var viewId = viewPath.split("/").slice(-2)[0];
  function ownName(value) {
    return value.indexOf(MARK) === 0 && value.indexOf("|") > 0 ? value.slice(value.indexOf("|") + 1) : value;
  }
  function setName(value) {
    try {
      window.name = value;
    } catch (e) {
      /* Read-only in this context */
    }
  }
  var stored = cfg.storage || {};
  var initialName = window.name;
  if (ownName(initialName) !== initialName) {
    var marker = initialName.slice(MARK.length, initialName.indexOf("|")).split(":");
    setName(ownName(initialName));
    var recent = Date.now() - Number(marker[2]) < 60000;
    if (marker[0] === viewId && marker[1] && recent && (cfg.writes || []).indexOf(marker[1]) < 0) {
      try {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", prefix + "/__lwu/storage?w=" + encodeURIComponent(marker[1]), false);
        xhr.send();
        if (xhr.status === 200) stored = JSON.parse(xhr.responseText);
      } catch (e) {
        /* Keep the copy we have */
      }
    }
  }
  var pageId = Math.random().toString(36).slice(2, 10);
  var sequence = 0;
  var lastWrite = null;
  var hiding = false;
  function rememberLastWrite() {
    if (lastWrite) setName(MARK + viewId + ":" + lastWrite[0] + ":" + lastWrite[1] + "|" + ownName(window.name));
  }
  // Requests with keepalive outlive the page, within 64 KiB for all of them
  var keepaliveBytes = 0;
  var encoder = new TextEncoder();

  function makeStorage(data, persist) {
    var changes = {};
    var cleared = false;
    var dirty = false;
    var resend = false; // A write was lost: send everything next time
    var inFlight = 0;
    var waiting = false;
    var timer = null;
    function schedule(delay) {
      if (!timer) timer = setTimeout(flush, delay);
    }
    function flush() {
      clearTimeout(timer);
      timer = null;
      if (!dirty) return;
      if (inFlight && !hiding) {
        waiting = true; // One write at a time, so they arrive in order
        return;
      }
      // Sent while another may still be on its way: send everything, so that
      // it does not matter which arrives first
      var full = resend || inFlight > 0;
      var id = Math.random().toString(36).slice(2, 12);
      var body = JSON.stringify({
        w: id,
        p: pageId,
        s: ++sequence,
        set: full ? Object.assign({}, data) : changes,
        clear: full || cleared,
      });
      changes = {};
      cleared = false;
      dirty = false;
      resend = false;
      lastWrite = [id, Date.now()];
      var size = encoder.encode(body).length;
      var keepalive = keepaliveBytes + size <= 60000;
      if (keepalive) keepaliveBytes += size;
      inFlight++;
      function done() {
        inFlight--;
        if (keepalive) keepaliveBytes -= size;
        if (waiting) {
          waiting = false;
          flush();
        }
      }
      function lost() {
        done();
        resend = true;
        dirty = true;
        if (!hiding) schedule(1000);
      }
      try {
        nativePost
          .call(window, prefix + "/__lwu/storage", {
            method: "POST",
            body: body,
            keepalive: keepalive,
            // A CORS-safelisted type: no preflight
            headers: { "Content-Type": "text/plain" },
          })
          .then(done, lost); // An HTTP error (too large) would fail again: not resent
      } catch (e) {
        lost();
      }
      if (hiding) rememberLastWrite();
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
      // After pagehide no timer will fire; large values do not wait either
      if (hiding || (value && value.length > 16384)) flush();
      else schedule(250);
    }
    if (persist) {
      window.addEventListener("pagehide", function () {
        hiding = true;
        flush();
        rememberLastWrite();
      });
      window.addEventListener("pageshow", function (event) {
        if (!event.persisted) return;
        hiding = false; // Back from the back/forward cache
        setName(ownName(window.name));
      });
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") flush();
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
    // Configurable, so that the Proxy may leave it out of Object.keys() and for...in
    Object.defineProperty(api, "length", {
      configurable: true,
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
