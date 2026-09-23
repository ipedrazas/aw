/* Small helpers, no build step. */
async function sendJSON(method, url, body) {
  const init = {method: method, headers: {"Content-Type": "application/json"}};
  if (body !== undefined) init.body = JSON.stringify(body || {});
  const r = await fetch(url, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ("Request failed (" + r.status + ")"));
  return data;
}
const postJSON = (url, body) => sendJSON("POST", url, body || {});
function say(el, text, isError) {
  if (!el) { if (isError) alert(text); return; }
  el.textContent = text; el.className = isError ? "small" : "small muted"; if (isError) el.style.color = "#9b2a1f"; else el.style.color = "";
}

/* Technical details switch, remembered per browser. */
(function () {
  const on = localStorage.getItem("wf.tech") === "1";
  if (on) document.body.classList.add("show-tech");
  document.querySelectorAll("[data-tech-switch]").forEach(cb => {
    cb.checked = on;
    cb.addEventListener("change", () => {
      document.body.classList.toggle("show-tech", cb.checked);
      localStorage.setItem("wf.tech", cb.checked ? "1" : "0");
    });
  });
})();

/* Delete a workflow or a draft. One confirm, then say where to go next. */
document.querySelectorAll("[data-delete]").forEach(b => b.addEventListener("click", async () => {
  if (!confirm(b.dataset.deleteConfirm || "Delete this? It cannot be undone.")) return;
  b.disabled = true;
  try { await sendJSON("DELETE", b.dataset.delete); location.href = b.dataset.after || location.href; }
  catch (err) { alert(err.message); b.disabled = false; }
}));

/* Rename a workflow: prompt for the new name, then go to its new URL. */
document.querySelectorAll("[data-rename]").forEach(b => b.addEventListener("click", async () => {
  const next = (prompt(b.dataset.renamePrompt || "New name:", b.dataset.renameCurrent || "") || "").trim();
  if (!next || next === b.dataset.renameCurrent) return;
  b.disabled = true;
  try { const d = await postJSON(b.dataset.rename, {name: next}); location.href = "/workflows/" + encodeURIComponent(d.name); }
  catch (err) { alert(err.message); b.disabled = false; }
}));

/* Toggle any element by id. */
document.querySelectorAll("[data-toggle]").forEach(b => {
  b.addEventListener("click", () => { const t = document.getElementById(b.dataset.toggle); if (t) t.classList.toggle("hidden"); });
});

/* Start a run of a saved workflow. */
document.querySelectorAll("form[data-run-workflow]").forEach(f => {
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const name = f.dataset.runWorkflow, msg = f.querySelector("[data-msg]");
    const topic = (f.querySelector("[name=topic]") || {}).value || "", caseName = (f.querySelector("[name=case]") || {}).value || "";
    const mode = (e.submitter && e.submitter.value) || "dry";
    const body = caseName ? {case: caseName, mode: mode} : {inputs: {topic: topic}, mode: mode};
    if (!caseName && topic.trim().length < 10) return say(msg, "Give a topic of at least ten characters, or pick a past case.", true);
    if (mode === "live" && !confirm("Run it for real? Follow-up research starts if the reviewer asks for it, so this can cost up to the workflow's budget. Nothing is sent anywhere.")) return;
    say(msg, mode === "live" ? "Starting the real run…" : "Starting…");
    try { const d = await postJSON("/api/workflows/" + encodeURIComponent(name) + "/runs", body); location.href = "/runs/" + d.run_id; }
    catch (err) { say(msg, err.message, true); }
  });
});

/* New draft from a description or a document. */
(function () {
  const f = document.querySelector("form[data-new-audit]"); if (!f) return;
  const ta = f.querySelector("textarea[name=document]"), msg = f.querySelector("[data-msg]");
  const submit = f.querySelector("button[type=submit]"), spinner = f.querySelector("[data-spinner]");
  f.querySelectorAll("[data-sample]").forEach(b => b.addEventListener("click", () => {
    const src = document.getElementById(b.dataset.sample); if (src) ta.value = src.textContent;
    const nm = f.querySelector("[name=name]"); if (nm && !nm.value) nm.value = "deep-research-process";
  }));
  f.addEventListener("submit", async e => {
    e.preventDefault();
    say(msg, "Reading your process and drafting the steps. This can take a minute…");
    submit.disabled = true;
    if (spinner) spinner.classList.remove("hidden");
    try { const d = await postJSON("/api/audits", {document: ta.value, name: (f.querySelector("[name=name]") || {}).value || null}); location.href = "/audits/" + d.id; }
    catch (err) {
      say(msg, err.message, true);
      submit.disabled = false;
      if (spinner) spinner.classList.add("hidden");
    }
  });
})();

/* Answer forms, on a draft and on a saved workflow: the same questions, posted to
   whichever one the page is about. */
function wireAnswers(base, reload) {
  document.querySelectorAll("form[data-answer]").forEach(f => {
    /* picking "No, I will answer this" opens a box to say what instead */
    const own = f.querySelector("[data-own]");
    if (own) f.querySelectorAll("input[type=radio]").forEach(r => r.addEventListener("change", () => {
      const no = (JSON.parse(f.querySelector("input[type=radio]:checked").value) || {}).keep === false;
      own.classList.toggle("hidden", !no);
      if (no) own.querySelector("textarea").focus();
    }));
    f.addEventListener("submit", async e => {
      e.preventDefault();
      const kind = f.dataset.kind, msg = f.querySelector("[data-msg]");
      let answer = null;
      if (kind === "choice" || kind === "bool") {
        const c = f.querySelector("input[type=radio]:checked");
        if (!c) return say(msg, "Pick one first, or chat about it if none fits.", true);
        answer = JSON.parse(c.value);
        const own = f.querySelector("[data-own]");
        if (own && answer && answer.keep === false) {
          const text = own.querySelector("textarea").value.trim();
          if (!text) return say(msg, "Say what it should be instead.", true);
          if (!window.chatAbout) return say(msg, "The chat is not available on this page.", true);
          say(msg, "Sent to the chat.");
          return window.chatAbout(f.dataset.answer, f.dataset.question, text);
        }
      } else if (kind === "multi") {
        answer = Array.from(f.querySelectorAll("input[type=checkbox]:checked")).map(c => JSON.parse(c.value));
        if (!answer.length) return say(msg, "Pick at least one.", true);
      } else if (kind === "number") {
        const v = f.querySelector("input[type=number]").value; if (v === "") return say(msg, "Enter a number.", true);
        answer = Number(v);
      } else {
        answer = f.querySelector("textarea").value; if (!answer.trim()) return say(msg, "Write something first.", true);
      }
      say(msg, "Saving…");
      try { await postJSON(base + "/answer", {finding_id: f.dataset.answer, answer: answer}); reload(msg); }
      catch (err) { say(msg, err.message, true); }
    });
  });
}

/* Workflow page: answer the questions still open on a saved workflow. */
(function () {
  const root = document.querySelector("[data-workflow-answers]"); if (!root) return;
  wireAnswers("/api/workflows/" + encodeURIComponent(root.dataset.workflowAnswers), () => location.reload());
})();

/* Audit page: chat, answers, undo, save, dry run. */
(function () {
  const root = document.querySelector("[data-audit]"); if (!root) return;
  const id = root.dataset.audit, base = "/api/audits/" + id;
  /* While the chat is thinking, a reload would throw its answer away: answers, undo and
     save are kept on the server at once, and the page refreshes when the chat is done. */
  let chatting = false;
  const reload = (msg) => {
    if (!chatting) return location.reload();
    say(msg, "Saved. The page updates when the chat answers.");
  };

  const chat = document.querySelector("form[data-chat]");
  if (chat) {
    const log = document.querySelector("[data-chat-log]"), box = chat.querySelector("textarea");
    const msg = chat.querySelector("[data-msg]");
    const aboutBar = chat.querySelector("[data-about]"), aboutText = chat.querySelector("[data-about-text]");
    let about = null;
    const toBottom = () => { if (log) log.scrollTop = log.scrollHeight; };
    const grow = () => { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 200) + "px"; };
    const bubble = (cls, text) => {
      const el = document.createElement("div"); el.className = "msg " + cls;
      if (text) el.textContent = text;
      log.appendChild(el); toBottom(); return el;
    };
    const setAbout = (id, question) => {
      about = id;
      aboutText.textContent = question || "";
      aboutBar.classList.toggle("hidden", !id);
    };
    toBottom();
    box.closest(".chat-box").addEventListener("click", () => box.focus());
    box.addEventListener("input", grow);
    box.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); chat.requestSubmit(); }
    });
    chat.querySelector("[data-about-clear]").addEventListener("click", () => { setAbout(null); box.focus(); });

    /* "Chat about this" on a question: the chat knows which one, and the box is ready. */
    document.querySelectorAll("[data-chat-about]").forEach(b => b.addEventListener("click", () => {
      setAbout(b.dataset.chatAbout, b.dataset.question);
      chat.scrollIntoView({behavior: "smooth", block: "nearest"});
      box.focus();
    }));

    /* "No, I will answer this": what they write goes to the chat about that question, which edits the draft. */
    window.chatAbout = (id, question, text) => {
      setAbout(id, question); box.value = text; grow(); chat.requestSubmit();
    };

    chat.addEventListener("submit", async e => {
      e.preventDefault();
      const text = box.value.trim();
      if (!text || box.readOnly) return;
      /* what was said shows at once, with a sign that an answer is coming */
      bubble("msg-user", text);
      const typing = bubble("msg-assistant msg-typing");
      typing.innerHTML = "<i></i><i></i><i></i>"; typing.setAttribute("aria-label", "Thinking");
      box.value = ""; grow(); box.readOnly = true; say(msg, ""); chatting = true;
      try { await postJSON(base + "/chat", {message: text, about: about}); chatting = false; location.reload(); }
      catch (err) {
        chatting = false;
        typing.remove(); box.value = text; grow(); box.readOnly = false;
        say(msg, err.message, true);
      }
    });
  }

  wireAnswers(base, reload);

  document.querySelectorAll("[data-reopen]").forEach(b => b.addEventListener("click", () => {
    const t = document.getElementById(b.dataset.reopen); if (t) { t.classList.remove("hidden"); b.classList.add("hidden"); }
  }));

  document.querySelectorAll("[data-undo]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await postJSON(base + "/undo", {seq: Number(b.dataset.undo)}); reload(); }
    catch (err) { alert(err.message); b.disabled = false; }
  }));

  const save = document.querySelector("[data-save]");
  if (save) save.addEventListener("click", async () => {
    save.disabled = true;
    try { await postJSON(base + "/save", {}); reload(); } catch (err) { alert(err.message); save.disabled = false; }
  });

  const tryForm = document.querySelector("form[data-try]");
  if (tryForm) tryForm.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = tryForm.querySelector("[data-msg]");
    const caseName = (tryForm.querySelector("[name=case]") || {}).value || "", topic = (tryForm.querySelector("[name=topic]") || {}).value || "";
    if (!caseName && topic.trim().length < 10) return say(msg, "Give a topic of at least ten characters, or pick a past case.", true);
    say(msg, "Saving and starting a dry run…");
    try {
      await postJSON(base + "/save", {});
      const d = await postJSON(base + "/dry-run", caseName ? {case: caseName} : {topic: topic});
      location.href = "/runs/" + d.run_id;
    } catch (err) { say(msg, err.message, true); }
  });

  document.querySelectorAll("[data-goto]").forEach(a => a.addEventListener("click", e => {
    const t = document.getElementById(a.dataset.goto); if (!t) return;
    e.preventDefault(); t.scrollIntoView({behavior: "smooth", block: "center"});
    t.classList.add("q-target"); setTimeout(() => t.classList.remove("q-target"), 2500);
  }));
})();

/* Run page: refresh while running. */
(function () {
  const el = document.querySelector("[data-run-refresh]"); if (!el) return;
  const id = el.dataset.runRefresh;
  setInterval(async () => {
    try { const r = await fetch("/api/runs/" + id); const d = await r.json(); if (d.status !== "running") location.reload(); else location.reload(); }
    catch (e) { /* try again next tick */ }
  }, 3000);
})();

/* Runs list: filter tabs and compare. */
(function () {
  const tabs = document.querySelectorAll("[data-filter]"); if (!tabs.length) return;
  tabs.forEach(t => t.addEventListener("click", () => {
    tabs.forEach(x => x.classList.remove("active")); t.classList.add("active");
    const want = t.dataset.filter;
    document.querySelectorAll("tr[data-status]").forEach(row => {
      const s = row.dataset.status, group = s === "done" ? "done" : s === "running" ? "running" : s === "waiting" ? "needs" : "stopped";
      row.classList.toggle("hidden", want !== "all" && group !== want);
    });
  }));
  const cmp = document.querySelector("form[data-compare]");
  if (cmp) cmp.addEventListener("submit", e => {
    e.preventDefault();
    const a = cmp.querySelector("[name=a]").value, b = cmp.querySelector("[name=b]").value;
    if (a && b && a !== b) location.href = "/runs/" + a + "/diff/" + b;
  });
})();

/* Compare select on a run page. */
document.querySelectorAll("select[data-compare-with]").forEach(s => s.addEventListener("change", () => {
  if (s.value) location.href = "/runs/" + s.dataset.compareWith + "/diff/" + s.value;
}));
