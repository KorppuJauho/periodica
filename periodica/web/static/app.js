// Small progressive enhancements. No inline scripts (strict CSP), no third-party code.
(function () {
  "use strict";

  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  function setResult(el, ok, message) {
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("ok", ok);
    el.classList.toggle("bad", !ok);
  }

  async function postForm(url, form, extra) {
    const body = new FormData(form);
    Object.entries(extra || {}).forEach(([key, value]) => body.set(key, value));
    const res = await fetch(url, {
      method: "POST",
      body,
      headers: { "X-CSRF-Token": csrf() },
      credentials: "same-origin",
      redirect: "error",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  // In-page confirmation for destructive forms (data-confirm="message"), instead of browser pop-ups.
  const dialog = document.getElementById("confirm-dialog");
  let pendingForm = null;
  document.addEventListener("submit", (event) => {
    const form = event.target;
    const message = form.getAttribute("data-confirm");
    if (!message || form.dataset.confirmed === "true") return;
    event.preventDefault();
    if (!dialog || typeof dialog.showModal !== "function") return;  // no dialog support: do nothing
    pendingForm = form;
    document.getElementById("confirm-message").textContent = message;
    dialog.showModal();
  });
  if (dialog) {
    document.getElementById("confirm-ok").addEventListener("click", (event) => {
      event.preventDefault();
      const form = pendingForm;
      pendingForm = null;
      dialog.close();
      if (form) {
        form.dataset.confirmed = "true";
        form.requestSubmit ? form.requestSubmit() : form.submit();
      }
    });
    document.getElementById("confirm-cancel").addEventListener("click", (event) => {
      event.preventDefault();
      pendingForm = null;
      dialog.close();
    });
    dialog.addEventListener("cancel", () => { pendingForm = null; });  // Esc key
  }

  // "Test connection" buttons.
  document.querySelectorAll("button[data-test]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = document.getElementById(button.dataset.form);
      const output = document.getElementById(button.dataset.output);
      button.disabled = true;
      setResult(output, true, "Testing…");
      try {
        const data = await postForm(button.dataset.test, form);
        setResult(output, data.ok, data.message || (data.ok ? "OK" : "Failed"));
        const list = document.getElementById("category-list");
        if (list && Array.isArray(data.categories)) {
          list.replaceChildren(...data.categories.map((c) => {
            const option = document.createElement("option");
            option.value = c;
            return option;
          }));
        }
        const select = document.getElementById("jellyfin-library");
        if (select && Array.isArray(data.libraries)) {
          const current = select.value;
          const options = [new Option("All libraries (slow on large servers)", "")];
          for (const lib of data.libraries) {
            const label = lib.name + (lib.type ? " (" + lib.type + ")" : "") +
              (lib.locations.length ? " – " + lib.locations.join(", ") : "");
            options.push(new Option(label, lib.id));
          }
          select.replaceChildren(...options);
          const wanted = current || data.suggested || "";
          if ([...select.options].some((o) => o.value === wanted)) select.value = wanted;
          select.dispatchEvent(new Event("change"));
        }
      } catch (err) {
        setResult(output, false, "Request failed: " + err.message);
      } finally {
        button.disabled = false;
      }
    });
  });

  // Keep the hidden library name in sync with the selected Jellyfin library.
  const librarySelect = document.getElementById("jellyfin-library");
  const libraryName = document.getElementById("jellyfin-library-name");
  if (librarySelect && libraryName) {
    librarySelect.addEventListener("change", () => {
      const option = librarySelect.options[librarySelect.selectedIndex];
      libraryName.value = option && option.value ? option.text.split(" (")[0].split(" – ")[0] : "";
    });
  }

  // Live title format preview.
  // Title formats (daily, monthly, numbered): one request previews all of them.
  const titleInputs = document.querySelectorAll("input[data-title-preview]");
  if (titleInputs.length) {
    const form = titleInputs[0].form;
    const outputs = {};
    titleInputs.forEach((input) => {
      outputs[input.dataset.titlePreview || "day"] = document.getElementById(input.dataset.previewOutput);
    });
    let timer;
    const update = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const data = await postForm("/settings-test/title", form);
          Object.entries(data.previews || {}).forEach(([period, result]) => {
            setResult(outputs[period], result.ok, result.message);
          });
        } catch (err) {
          Object.values(outputs).forEach((output) => setResult(output, false, ""));
        }
      }, 300);
    };
    titleInputs.forEach((input) => input.addEventListener("input", update));
    form.querySelector('input[name="language"]')?.addEventListener("input", update);
    update();
  }

  // Dashboard: while a scan is queued or running, poll its status and reload when it has finished.
  const scanForm = document.getElementById("scan-form");
  if (scanForm && scanForm.dataset.scanPending === "true") {
    let polls = 0;
    const poll = async () => {
      polls += 1;
      try {
        const res = await fetch("/scan/status", { credentials: "same-origin", redirect: "error" });
        if (res.ok) {
          const data = await res.json();
          // "queued" stays true until the scheduler picks up this request, so a scan that was
          // already running when the button was pressed doesn't end the wait early.
          if (!data.running && !data.queued) {
            window.location.replace("/");  // drops ?msg=scan so the banner doesn't linger
            return;
          }
        }
      } catch (err) {
        // transient error: keep polling
      }
      if (polls < 900) setTimeout(poll, 2000);  // give up after ~30 min
    };
    setTimeout(poll, 1500);
  }

  // Download client: show the parts of the form that belong to the chosen mode. Disabled fields are
  // not submitted, so the server keeps the saved qBittorrent values while folder mode is chosen.
  const modeSwitches = document.querySelectorAll("input[data-mode-switch]");
  if (modeSwitches.length) {
    const applyMode = () => {
      const checked = document.querySelector("input[data-mode-switch]:checked");
      const mode = checked ? checked.value : "qbittorrent";
      document.querySelectorAll("[data-mode]").forEach((el) => {
        const active = el.dataset.mode === mode;
        if (el.tagName === "FIELDSET") {
          el.disabled = !active;
        } else {
          el.hidden = !active;
        }
      });
    };
    modeSwitches.forEach((input) => input.addEventListener("change", applyMode));
    applyMode();
  }

  // Library form (add wizard and edit page): suggestions from qBittorrent/Jellyfin, live checks, dry run.
  // A field the user has typed in (or that had a value when the page opened) is never overwritten.
  const libraryForm = document.getElementById("library-form");
  if (libraryForm) {
    const field = (name) => libraryForm.querySelector('[name="' + name + '"]');
    const touched = new Set();
    ["name", "category", "source_dir", "dest_dir"].forEach((name) => {
      const input = field(name);
      if (!input) return;
      if (input.value) touched.add(name);
      input.addEventListener("input", () => touched.add(name));
    });
    const jfSelect = document.getElementById("jellyfin-library");
    if (jfSelect && jfSelect.value) touched.add("jellyfin");
    jfSelect?.addEventListener("change", () => touched.add("jellyfin"));

    const text = (tag, value, className) => {
      const el = document.createElement(tag);
      el.textContent = value;
      if (className) el.className = className;
      return el;
    };
    const hint = (id, message, ok) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = message || "";
      el.classList.toggle("bad", ok === false);
    };

    let refills = 0;
    const apply = (data) => {
      const suggested = data.suggested || {};
      let filled = false;
      for (const name of ["category", "source_dir", "dest_dir"]) {
        const input = field(name);
        if (input && !touched.has(name) && suggested[name] && input.value !== suggested[name]) {
          input.value = suggested[name];
          filled = true;
        }
      }
      // The checks were made for the values before the suggestions: ask once more for the filled-in ones.
      if (filled && refills < 3) {
        refills += 1;
        suggest();
      } else {
        refills = 0;
      }
      // Categories from qBittorrent, with their save paths.
      const list = document.getElementById("category-options");
      const categories = Array.isArray(data.categories) ? data.categories : null;
      if (list && categories) {
        list.replaceChildren(...categories.filter((c) => !c.used_by).map((c) => new Option(c.local_path || "", c.name)));
      }
      const wanted = field("category")?.value || "";
      if (data.mode === "offline") {
        hint("category-hint", "Offline mode: Periodica does not contact qBittorrent.");
      } else if (data.categories_error) {
        hint("category-hint", "Could not read qBittorrent's categories: " + data.categories_error, false);
      } else if (categories) {
        const match = categories.find((c) => c.name === wanted);
        if (!wanted) {
          hint("category-hint", categories.length + " categories in qBittorrent.");
        } else if (!match) {
          hint("category-hint", "Category '" + wanted + "' does not exist in qBittorrent yet. Create it there " +
            "(right-click in the category list → Add category, with a save path), then press Reload.", false);
        } else if (match.used_by) {
          hint("category-hint", "Already used by library '" + match.used_by + "'.", false);
        } else {
          hint("category-hint", "qBittorrent saves '" + wanted + "' to " + (match.save_path || "its default folder") +
            (match.local_path ? " (" + match.local_path + " here)." : "."));
        }
      }
      // Jellyfin libraries, with the one matching the destination preselected.
      const jf = data.jellyfin || {};
      if (jfSelect && Array.isArray(jf.libraries)) {
        const current = jfSelect.value;
        const wasTouched = touched.has("jellyfin");
        const options = [new Option("All libraries (slow on large servers)", "")];
        for (const lib of jf.libraries) {
          const label = lib.name + (lib.type ? " (" + lib.type + ")" : "") +
            (lib.locations.length ? " – " + lib.locations.join(", ") : "");
          options.push(new Option(label, lib.id));
        }
        jfSelect.replaceChildren(...options);
        const pick = wasTouched ? current : (jf.suggested || current);
        if ([...jfSelect.options].some((o) => o.value === pick)) jfSelect.value = pick;
        jfSelect.dispatchEvent(new Event("change"));   // updates the hidden library name
        if (!wasTouched) touched.delete("jellyfin");   // a suggestion is not a choice
        if (jf.suggested) {
          hint("jellyfin-hint", "Matched by folder: " + jfSelect.options[jfSelect.selectedIndex].text);
        } else {
          hint("jellyfin-hint", "No Jellyfin library points at the destination yet. Create a Books library in " +
            "Jellyfin for " + (jf.path_hint || "the destination folder") + ", untick the online metadata " +
            "downloaders, then press Reload.", false);
        }
      } else if (jf.error) {
        hint("jellyfin-hint", "Could not read Jellyfin's libraries: " + jf.error, false);
      }
      // Checks.
      const checks = document.getElementById("library-checks");
      if (checks) {
        checks.replaceChildren(...(data.checks || []).map((c) => {
          const level = c.level || (c.ok ? "ok" : "bad");
          const li = text("li", "", level);
          li.append(text("span", { ok: "✓ ", warn: "⚠ ", bad: "✗ " }[level] || "• "), text("span", c.label));
          if (c.detail) li.append(text("span", " — " + c.detail, "muted small"));
          return li;
        }));
      }
    };

    let timer;
    let running = 0;
    const suggest = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++running;
        try {
          const auto = ["category", "source_dir", "dest_dir"].filter((name) => !touched.has(name));
          const data = await postForm("/settings/libraries/suggest", libraryForm, { auto_fields: auto.join(",") });
          if (mine === running) apply(data);
        } catch (err) {
          hint("category-hint", "Suggestions unavailable: " + err.message, false);
        }
      }, 300);
    };
    ["name", "category", "source_dir", "dest_dir"].forEach((name) => field(name)?.addEventListener("input", suggest));
    libraryForm.querySelectorAll("[data-wizard-reload]").forEach((button) => button.addEventListener("click", suggest));
    suggest();

    const previewButton = libraryForm.querySelector("[data-library-preview]");
    previewButton?.addEventListener("click", async () => {
      const output = document.getElementById("library-preview");
      previewButton.disabled = true;
      setResult(output, true, "Running a dry run…");
      try {
        const data = await postForm("/settings/libraries/preview", libraryForm);
        setResult(output, data.ok, data.message);
      } catch (err) {
        setResult(output, false, "Request failed: " + err.message);
      } finally {
        previewButton.disabled = false;
      }
    });
  }

  // Naming pattern form: live preview of what the file (and other unmatched files) become.
  const patternForm = document.getElementById("pattern-form");
  if (patternForm) {
    const libraryId = patternForm.action.split("/libraries/")[1].split("/")[0];
    const output = document.getElementById("pattern-result");
    const matches = document.getElementById("pattern-matches");
    let timer;
    const preview = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const data = await postForm("/settings/libraries/" + libraryId + "/patterns/preview", patternForm);
          setResult(output, data.ok, data.message || "");
          matches.replaceChildren(...(data.matches || []).map((m) => {
            const li = document.createElement("li");
            li.textContent = "Also matches: " + m;
            return li;
          }));
        } catch (err) {
          setResult(output, false, "Preview unavailable: " + err.message);
        }
      }, 250);
    };
    patternForm.querySelectorAll("[data-pattern-field]").forEach((input) => input.addEventListener("input", preview));
    preview();
  }

  // Reveal hidden values (API key).
  document.querySelectorAll("button[data-reveal]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.reveal);
      if (target) {
        target.hidden = !target.hidden;
        button.textContent = target.hidden ? "Show API key" : "Hide API key";
      }
    });
  });
  // Logs: keep the page live by asking for new lines instead of reloading. Pause stops rendering,
  // not polling, so nothing is missed while reading.
  const logPre = document.getElementById("log-lines");
  const liveButton = document.getElementById("log-live");
  if (logPre && liveButton) {
    const NL = String.fromCharCode(10);
    const MAX_ROWS = 1000;
    const missedBox = document.getElementById("log-missed");
    const stateLabel = document.getElementById("log-live-state");
    const debugBadge = document.getElementById("log-debug-left");
    const emptyNote = document.getElementById("log-empty");
    let cursor = parseInt(logPre.dataset.cursor || "0", 10);
    const shownDetail = logPre.dataset.detail || "";
    const shownTemporary = logPre.dataset.temporary === "true";
    let paused = false;
    let pending = [];
    let missed = 0;

    const rowFor = (line) => {
      const span = document.createElement("span");
      span.className = "lvl-" + line.level;
      const level = (line.level || "").toUpperCase().padEnd(7);
      span.textContent = line.ts + "  " + level + " " + line.logger + ": " + line.message + NL;
      return span;
    };

    const render = (lines) => {
      lines.forEach((line) => logPre.insertBefore(rowFor(line), logPre.firstChild));
      while (logPre.childElementCount > MAX_ROWS) logPre.removeChild(logPre.lastElementChild);
      if (lines.length && emptyNote) emptyNote.hidden = true;
    };

    const showMissed = () => {
      if (!missedBox || !missed) return;
      missedBox.hidden = false;
      missedBox.textContent = missed + " line(s) scrolled out of the buffer before they could be shown.";
    };

    const setPaused = (value) => {
      paused = value;
      liveButton.textContent = paused ? (pending.length ? "Resume (" + pending.length + " new)" : "Resume") : "Pause";
      if (stateLabel) stateLabel.textContent = paused ? "Paused" : "Live";
      if (!paused && pending.length) {
        render(pending);
        pending = [];
        showMissed();
        liveButton.textContent = "Pause";
      }
    };

    liveButton.addEventListener("click", () => setPaused(!paused));

    let timer = null;
    const schedule = (delay) => {
      if (timer) clearTimeout(timer);
      // A hidden tab still follows along, just lazily; it catches up as soon as it is shown again.
      timer = setTimeout(poll, delay === undefined ? (document.hidden ? 10000 : 2000) : delay);
    };

    const poll = async () => {
      try {
        const res = await fetch(liveButton.dataset.url + "&after=" + cursor,
                                { credentials: "same-origin", redirect: "error" });
        if (res.ok) {
          const data = await res.json();
          cursor = data.cursor || cursor;
          if (data.missed) missed += data.missed;
          if (data.lines && data.lines.length) {
            if (paused) {
              pending = pending.concat(data.lines).slice(-MAX_ROWS);
              liveButton.textContent = "Resume (" + pending.length + " new)";
            } else {
              render(data.lines);
              showMissed();
            }
          }
          // The controls were rendered for one state; when that state changes (a temporary debug
          // session ran out, or the level was changed elsewhere) the page has to be re-rendered.
          if (data.detail !== shownDetail || data.temporary !== shownTemporary) {
            window.location.reload();
            return;
          }
          if (debugBadge && data.temporary) {
            const minutes = Math.ceil((data.debug_seconds_left || 0) / 60);
            debugBadge.textContent = "debug · " + minutes + " min left";
          }
        }
      } catch (err) {
        // transient error (restart, network): keep polling
      }
      schedule();
    };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(200); });
    schedule(2000);
  }

})();
