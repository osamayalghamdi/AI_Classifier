/* New-incident page: write incident + attachment OCR (result shown ON the
 * page, editable) + classify. Relative API paths — works through the nginx
 * proxy (tunnel or compose); localStorage overrides still honored. */
(function () {
  "use strict";

  var API = localStorage.getItem("dash_api") || "";
  var OCR_URL = API + "/ocr";        // proxied to the OCR service by nginx
  var CLASSIFY_URL = API + "/classify";

  var el = function (id) { return document.getElementById(id); };
  var title = el("incTitle"), desc = el("incDesc"),
      fileInput = el("incFile"), drop = el("fileDrop"),
      fileName = el("fileName"), ocrBox = el("ocrBox"),
      ocrLabel = el("ocrLabel"), ocrDot = el("ocrDot"), ocrText = el("ocrText"),
      submitBtn = el("submitBtn"), spinner = el("spinner"), msg = el("msg"),
      result = el("result");

  var chosenFile = null;
  var ocrRun = 0; // token to ignore stale OCR responses

  function setMsg(text, kind) {
    msg.textContent = text || "";
    msg.style.display = text ? "block" : "none";
    msg.className = "msg " + (kind || "");
  }

  function busy(on) {
    submitBtn.disabled = on;
    spinner.style.display = on ? "inline-block" : "none";
  }

  // ── File picker ────────────────────────────────────────────────
  drop.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    chosenFile = fileInput.files[0] || null;
    if (chosenFile) {
      drop.classList.add("has-file");
      fileName.textContent = "✓ " + chosenFile.name + " (" + (chosenFile.size / 1024).toFixed(0) + " KB)";
      runOcr(chosenFile); // extract immediately — result shown right here
    } else {
      drop.classList.remove("has-file");
      fileName.textContent = "";
      ocrBox.style.display = "none";
      ocrText.value = "";
    }
  });

  // ── OCR the attachment (en + ar), fill the editable box ─────────
  function runOcr(file) {
    var token = ++ocrRun;
    ocrBox.style.display = "block";
    ocrLabel.textContent = "Reading text from attachment (OCR)…";
    ocrLabel.classList.remove("error");
    ocrDot.classList.add("busy");
    ocrText.value = "";
    var fd = new FormData();
    fd.append("file", file);
    fd.append("lang", "both");
    fetch(OCR_URL, { method: "POST", body: fd })
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.detail || ("OCR failed " + r.status)); }); })
      .then(function (data) {
        if (token !== ocrRun) return; // a newer file was chosen meanwhile
        ocrDot.classList.remove("busy");
        var text = (data.text || "").trim();
        ocrText.value = text;
        if (text) {
          ocrLabel.textContent = "Text read from attachment (OCR) — you can edit it";
          if (data.has_low_confidence) ocrLabel.textContent += " (some words low confidence)";
        } else {
          ocrLabel.textContent = "No text found in the attachment — you can still submit without it";
        }
      })
      .catch(function (err) {
        if (token !== ocrRun) return;
        ocrDot.classList.remove("busy");
        ocrLabel.classList.add("error");
        ocrLabel.textContent = "OCR failed: " + (err.message || "try another file");
      });
  }

  // ── Submit ─────────────────────────────────────────────────────
  submitBtn.addEventListener("click", function () {
    if (!title.value.trim()) { setMsg("Title is required", "error"); title.focus(); return; }
    setMsg("");
    busy(true);
    result.style.display = "none";

    var payload = {
      title: title.value.trim(),
      description: desc.value.trim(),
      extracted_text: ocrText.value.trim() // editable OCR result from this page
    };

    fetch(CLASSIFY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.detail || ("Classification failed " + r.status)); });
      })
      .then(function (data) {
        var c = data.classification || {};
        el("resTitle").textContent = data.incident_title || payload.title;
        el("resId").textContent = data.incident_id || "";
        el("resSystem").textContent = c.affected_system || "—";
        el("resService").textContent = c.service || "—";
        el("resType").textContent = c.incident_type || "—";
        var sev = c.severity || "—";
        var sevEl = el("resSev");
        sevEl.textContent = sev;
        sevEl.className = "sev-pill sev-" + sev;
        el("resConf").textContent = c.confidence || "—";
        if (payload.extracted_text) {
          el("resOcrText").textContent = payload.extracted_text;
          el("resOcr").style.display = "block";
        }
        el("resNote").innerHTML =
          'Saved — <a href="index.html#incident=' + encodeURIComponent(data.incident_id || "") + '">open it in the dashboard</a>';
        result.style.display = "block";
        result.scrollIntoView({ behavior: "smooth", block: "nearest" });
        setMsg("", "");
        // reset form for the next incident
        title.value = ""; desc.value = ""; chosenFile = null;
        fileInput.value = ""; drop.classList.remove("has-file"); fileName.textContent = "";
        ocrBox.style.display = "none"; ocrText.value = "";
      })
      .catch(function (err) { setMsg(err.message || "Something went wrong", "error"); })
      .then(function () { busy(false); });
  });

  // Enter submits on the title field
  title.addEventListener("keydown", function (e) { if (e.key === "Enter") submitBtn.click(); });
})();
