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

/* What you typed and have not sent yet survives a reload of the page, for this tab.
   Answering one question reloads the page; the chat you were halfway through, or the
   answer you were writing to another question, should still be there. */
const kept = {
  get(key) { try { return JSON.parse(sessionStorage.getItem("kept:" + key) || "null"); } catch (e) { return null; } },
  set(key, value) { try { sessionStorage.setItem("kept:" + key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
  drop(key) { try { sessionStorage.removeItem("kept:" + key); } catch (e) { /* private mode */ } },
};
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
    const field = f.querySelector("textarea:not([data-own] textarea), input[type=number]");
    const keyAnswer = "answer:" + base + ":" + f.dataset.answer;
    if (field) {
      const unsent = kept.get(keyAnswer);
      if (unsent && !field.value) field.value = unsent;
      field.addEventListener("input", () => field.value.trim() ? kept.set(keyAnswer, field.value) : kept.drop(keyAnswer));
    }
    /* picking "No, I will answer this" opens a box to say what instead */
    const own = f.querySelector("[data-own]");
    /* ("No, the default is enough" says what instead by itself, so it needs no box) */
    const saysNo = v => v && v.keep === false && !v.then;
    if (own) f.querySelectorAll("input[type=radio]").forEach(r => r.addEventListener("change", () => {
      const no = saysNo(JSON.parse(f.querySelector("input[type=radio]:checked").value));
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
        if (own && saysNo(answer)) {
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
      /* the same answer for the ticked steps that are asked the same question */
      const also = Array.from(f.querySelectorAll("input[data-also]:checked")).map(c => c.value);
      try {
        const d = await postJSON(base + "/answer", {finding_id: f.dataset.answer, answer: answer, also: also, from_chat: "fromChat" in f.dataset});
        kept.drop(keyAnswer);
        if (d.not_taken && d.not_taken.length) alert("Some steps did not take this answer and are still open:\n\n" + d.not_taken.join("\n"));
        reload(msg);
      }
      catch (err) { say(msg, err.message, true); }
    });
  });
}

/* Workflow page: answer the questions still open on a saved workflow. */
(function () {
  const root = document.querySelector("[data-workflow-answers]"); if (!root) return;
  wireAnswers("/api/workflows/" + encodeURIComponent(root.dataset.workflowAnswers), () => location.reload());
})();

/* Workflow page: the model each step runs on, and the default for the rest. */
(function () {
  const root = document.querySelector("[data-models]"); if (!root) return;
  const url = "/api/workflows/" + encodeURIComponent(root.dataset.models) + "/model";
  const msg = root.querySelector("[data-msg]");
  const choose = async (sel, step) => {
    if (sel.dataset.resets && !confirm("Changing the model of “" + sel.dataset.title + "” starts its count of accepted runs again, so it asks you before running on its own.")) {
      sel.value = sel.dataset.was; return;
    }
    say(msg, "Saving…");
    try { await postJSON(url, {step: step, model: sel.value || null}); location.reload(); }
    catch (err) { sel.value = sel.dataset.was; say(msg, err.message, true); }
  };
  root.querySelector("[data-model-default]").addEventListener("change", e => choose(e.target, null));
  document.querySelectorAll("[data-model-step]").forEach(sel => {
    sel.dataset.was = sel.value;
    sel.addEventListener("change", () => choose(sel, sel.dataset.modelStep));
  });
  const d = root.querySelector("[data-model-default]"); d.dataset.was = d.value;
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
    const keyChat = "chat:" + id;
    const keep = () => box.value.trim() ? kept.set(keyChat, {text: box.value, about: about, question: aboutText.textContent}) : kept.drop(keyChat);
    /* what they type next is about the question the chat just asked, unless they say otherwise */
    if (chat.dataset.asking) setAbout(chat.dataset.asking, chat.dataset.askingQuestion);
    const unsent = kept.get(keyChat);
    if (unsent && !box.value) { box.value = unsent.text || ""; if (unsent.about) setAbout(unsent.about, unsent.question); grow(); }

    document.querySelectorAll("[data-skip]").forEach(b => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await postJSON(base + "/skip", {finding_id: b.dataset.skip}); reload(msg); }
      catch (err) { say(msg, err.message, true); b.disabled = false; }
    }));
    box.closest(".chat-box").addEventListener("click", () => box.focus());
    box.addEventListener("input", () => { grow(); keep(); });
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
      box.value = ""; grow(); box.readOnly = true; say(msg, ""); chatting = true; kept.drop(keyChat);
      try { await postJSON(base + "/chat", {message: text, about: about}); chatting = false; location.reload(); }
      catch (err) {
        chatting = false;
        typing.remove(); box.value = text; grow(); box.readOnly = false; keep();
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

/* Sections you opened stay open across a reload: each <details> is remembered by the
   card it sits in and its place there, for this page, for this tab. */
(function () {
  const key = "open:" + location.pathname;
  const id = d => {
    const card = d.closest(".card"), head = card && card.querySelector("h2, h3");
    const same = card ? Array.from(card.querySelectorAll("details")) : [];
    return (head ? head.textContent.trim() : "") + "#" + same.indexOf(d);
  };
  let open = [];
  try { open = JSON.parse(sessionStorage.getItem(key) || "[]"); } catch (e) { open = []; }
  document.querySelectorAll("details").forEach(d => {
    if (open.includes(id(d))) d.open = true;
    d.addEventListener("toggle", () => {
      const now = Array.from(document.querySelectorAll("details")).filter(x => x.open).map(id);
      try { sessionStorage.setItem(key, JSON.stringify(now)); } catch (e) { /* private mode */ }
    });
  });
})();

/* Run page: refresh while running, only when something changed, and never while you
   are typing. */
(function () {
  const el = document.querySelector("[data-run-refresh]"); if (!el) return;
  const id = el.dataset.runRefresh;
  const shape = d => d.status + "|" + (d.steps || []).map(s =>
    s.step_id + ":" + s.status + ":" + (s.decisions || []).length).join(",");
  let seen = null;
  const tick = async () => {
    try {
      const d = await (await fetch("/api/runs/" + id)).json();
      const now = shape(d);
      if (seen === null) seen = now;
      const typing = document.activeElement && /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement.tagName);
      if (now !== seen && !typing) location.reload();
    } catch (e) { /* try again next tick */ }
  };
  tick();
  setInterval(tick, 3000);
})();

/* Run page: answer the step a real run is waiting at, and carry on. */
document.querySelectorAll("form[data-answer-wait]").forEach(f => {
  const topics = f.querySelector("[data-topics]");
  f.querySelectorAll("input[name=go]").forEach(r => r.addEventListener("change", () => {
    topics.classList.toggle("hidden", f.querySelector("input[name=go]:checked").value !== "deeper");
    if (!topics.classList.contains("hidden")) topics.querySelector("textarea").focus();
  }));
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = f.querySelector("[data-msg]"), c = f.querySelector("input[name=go]:checked");
    if (!c) return say(msg, "Pick one first.", true);
    const deeper = c.value === "deeper";
    const list = deeper ? topics.querySelector("textarea").value : "";
    if (deeper && !list.trim()) return say(msg, "Say what to go deeper into, one topic per line.", true);
    say(msg, "Carrying on…");
    try {
      await postJSON("/api/runs/" + f.dataset.answerWait + "/answer",
        {go_deeper: deeper, topics: list, note: (f.querySelector("[name=note]") || {}).value || ""});
      location.reload();
    } catch (err) { say(msg, err.message, true); }
  });
});

/* Run page: pick a run that broke up again, at the step that broke. */
document.querySelectorAll("[data-retry]").forEach(box => {
  const btns = box.querySelectorAll("button[data-then]"), msg = box.querySelector("[data-msg]");
  btns.forEach(btn => btn.addEventListener("click", async () => {
    const then = btn.dataset.then;
    btns.forEach(b => b.disabled = true);
    say(msg, then === "skip" ? "Carrying on without it…" : "Picking it up…");
    try { await postJSON("/api/runs/" + box.dataset.retry + "/" + then, {}); location.reload(); }
    catch (err) { btns.forEach(b => b.disabled = false); say(msg, err.message, true); }
  }));
});

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

/* Edit a step's instructions: save as a new version, then show the step's page for it.
   What you typed and have not saved survives a reload, like an unsent answer. */
document.querySelectorAll("form[data-skill-edit]").forEach(f => {
  const box = f.querySelector("textarea[name=body]"), msg = f.querySelector("[data-msg]");
  const keyBody = "skill:" + f.dataset.skillEdit + ":" + f.dataset.latest;
  const draft = kept.get(keyBody);
  if (draft && draft !== box.value) { box.value = draft; f.closest(".hidden")?.classList.remove("hidden"); }
  box.addEventListener("input", () => kept.set(keyBody, box.value));
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const b = f.querySelector("button[type=submit]"); b.disabled = true; say(msg, "Saving…");
    try {
      const d = await postJSON(f.dataset.skillEdit, {body: box.value, latest: Number(f.dataset.latest)});
      kept.drop(keyBody);
      say(msg, "Saved as version " + d.version + ".");
      location.href = f.dataset.after;
    } catch (err) { say(msg, err.message, true); b.disabled = false; }
  });
});
