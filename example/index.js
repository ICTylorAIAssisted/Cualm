(function () {
  "use strict";

  // ─── State ───
  const state = {
    counter: 0,
    tasks: [],
    taskIdSeq: 0,
    theme: "dark",
    activeTab: "tab1",
    settings: { notify: false, sound: false, autoSave: false },
    accentColor: "#e94560",
    searchFilter: "all",
    dropCount: 0,
    logEntries: [],
  };

  // ─── Utility ───
  function $(sel) {
    return document.querySelector(sel);
  }
  function $$(sel) {
    return document.querySelectorAll(sel);
  }

  function log(msg) {
    const time = new Date().toLocaleTimeString();
    const entry = `[${time}] ${msg}`;
    state.logEntries.push(entry);
    const el = $("#eventLog");
    el.textContent += "\n" + entry;
    el.scrollTop = el.scrollHeight;
  }

  function toast(message, type) {
    type = type || "info";
    const container = $("#toastContainer");
    const el = document.createElement("div");
    el.className = "toast toast-" + type;
    el.textContent = message;
    container.appendChild(el);
    log("Toast [" + type + "]: " + message);
    setTimeout(function () {
      if (el.parentNode) {
        el.parentNode.removeChild(el);
      }
    }, 3000);
  }

  function updateStatus(msg) {
    $("#statusBar").textContent = msg;
  }

  // ─── Modal ───
  let modalResolve = null;

  function showModal(title, body) {
    $("#modalTitle").textContent = title;
    $("#modalBody").textContent = body;
    $("#modalOverlay").classList.add("open");
    log("Modal opened: " + title);
    return new Promise(function (resolve) {
      modalResolve = resolve;
    });
  }

  function closeModal(result) {
    $("#modalOverlay").classList.remove("open");
    log("Modal closed: " + result);
    if (modalResolve) {
      modalResolve(result);
      modalResolve = null;
    }
  }

  $("#modalConfirm").addEventListener("click", function () {
    closeModal(true);
  });
  $("#modalCancel").addEventListener("click", function () {
    closeModal(false);
  });
  $("#modalOverlay").addEventListener("click", function (e) {
    if (e.target === $("#modalOverlay")) {
      closeModal(false);
    }
  });

  // ─── Counter ───
  function updateCounter() {
    $("#counterValue").textContent = state.counter;
    var status;
    if (state.counter > 0) {
      status = "Positive";
    } else if (state.counter < 0) {
      status = "Negative";
    } else {
      status = "Zero";
    }
    $("#counterStatus").textContent = "Value: " + status;
    log("Counter → " + state.counter);
  }

  $("#btnIncrement").addEventListener("click", function () {
    state.counter++;
    updateCounter();
  });

  $("#btnDecrement").addEventListener("click", function () {
    state.counter--;
    updateCounter();
  });

  $("#btnDouble").addEventListener("click", function () {
    if (state.counter === 0) {
      toast("Cannot double zero", "warn");
    } else {
      state.counter *= 2;
      updateCounter();
      toast("Doubled to " + state.counter, "success");
    }
  });

  $("#btnReset").addEventListener("click", function () {
    state.counter = 0;
    updateCounter();
    toast("Counter reset", "info");
  });

  // ─── Tasks ───
  function renderTasks() {
    const list = $("#taskList");
    list.innerHTML = "";
    state.tasks.forEach(function (task) {
      const li = document.createElement("li");
      li.setAttribute("draggable", "true");
      li.dataset.id = task.id;
      if (task.done) li.classList.add("done");

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = task.done;
      cb.addEventListener("change", function () {
        task.done = cb.checked;
        renderTasks();
        log(
          'Task "' +
            task.text +
            '" ' +
            (task.done ? "completed" : "uncompleted"),
        );
      });

      const span = document.createElement("span");
      span.textContent = task.text;
      span.style.flex = "1";

      const tag = document.createElement("span");
      tag.className = "tag tag-" + task.priority;
      tag.textContent = task.priority;

      const del = document.createElement("button");
      del.className = "small";
      del.textContent = "✕";
      del.addEventListener("click", function () {
        state.tasks = state.tasks.filter(function (t) {
          return t.id !== task.id;
        });
        renderTasks();
        toast("Task deleted", "info");
      });

      li.appendChild(cb);
      li.appendChild(span);
      li.appendChild(tag);
      li.appendChild(del);

      // Drag events
      li.addEventListener("dragstart", function (e) {
        li.classList.add("dragging");
        e.dataTransfer.setData("text/plain", task.id.toString());
        e.dataTransfer.effectAllowed = "move";
        log("Drag start: " + task.text);
      });
      li.addEventListener("dragend", function () {
        li.classList.remove("dragging");
      });

      list.appendChild(li);
    });

    const total = state.tasks.length;
    const done = state.tasks.filter(function (t) {
      return t.done;
    }).length;
    if (total === 0) {
      $("#taskCount").textContent = "No tasks yet";
    } else {
      $("#taskCount").textContent = done + "/" + total + " tasks completed";
    }
  }

  function addTask() {
    const input = $("#taskInput");
    const text = input.value.trim();
    if (!text) {
      toast("Task text is required", "error");
      return;
    }
    if (text.length > 100) {
      toast("Task too long (max 100 chars)", "error");
      return;
    }
    const priority = $("#taskPriority").value;
    state.taskIdSeq++;
    state.tasks.push({
      id: state.taskIdSeq,
      text: text,
      priority: priority,
      done: false,
    });
    input.value = "";
    renderTasks();
    toast("Task added: " + text, "success");
  }

  $("#btnAddTask").addEventListener("click", addTask);
  $("#taskInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      addTask();
    }
  });

  // ─── Form Validation ───
  function validateForm() {
    const errors = [];
    const name = $("#formName").value.trim();
    const email = $("#formEmail").value.trim();
    const age = $("#formAge").value;
    const category = $("#formCategory").value;
    const agree = $("#formAgree").checked;

    if (!name) {
      errors.push("Name is required");
    } else if (name.length < 2) {
      errors.push("Name must be at least 2 characters");
    }

    if (!email) {
      errors.push("Email is required");
    } else if (email.indexOf("@") === -1 || email.indexOf(".") === -1) {
      errors.push("Email format is invalid");
    }

    if (!age) {
      errors.push("Age is required");
    } else if (parseInt(age) < 18) {
      errors.push("Must be at least 18");
    } else if (parseInt(age) > 120) {
      errors.push("Age seems unrealistic");
    }

    if (!category) {
      errors.push("Please select a category");
    }

    if (!agree) {
      errors.push("You must agree to terms");
    }

    return errors;
  }

  $("#btnSubmitForm").addEventListener("click", function () {
    const errors = validateForm();
    const errDiv = $("#formErrors");
    if (errors.length > 0) {
      errDiv.style.color = "var(--error)";
      errDiv.textContent = errors.join(". ") + ".";
      toast("Form has " + errors.length + " error(s)", "error");
      log("Form validation failed: " + errors.length + " errors");
    } else {
      errDiv.style.color = "var(--success)";
      errDiv.textContent = "Form submitted successfully!";
      toast("Form submitted!", "success");
      log("Form submitted successfully");
    }
  });

  // ─── Tabs ───
  $$(".tabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tabId = btn.dataset.tab;
      $$(".tabs button").forEach(function (b) {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      $$(".tab-panel").forEach(function (p) {
        p.classList.remove("active");
      });
      $("#" + tabId).classList.add("active");
      state.activeTab = tabId;
      log("Tab switched to: " + tabId);
    });
  });

  // Progress slider
  $("#progressSlider").addEventListener("input", function () {
    var val = this.value;
    $("#progressFill").style.width = val + "%";
    $("#progressLabel").textContent = val + "%";
    if (parseInt(val) === 100) {
      toast("Progress complete!", "success");
    }
    log("Progress: " + val + "%");
  });

  // Color swatches
  $$(".color-swatch").forEach(function (swatch) {
    swatch.addEventListener("click", function () {
      $$(".color-swatch").forEach(function (s) {
        s.classList.remove("selected");
      });
      swatch.classList.add("selected");
      state.accentColor = swatch.dataset.color;
      document.documentElement.style.setProperty(
        "--accent",
        swatch.dataset.color,
      );
      toast("Accent color changed", "info");
      log("Color changed to " + swatch.dataset.color);
    });
  });

  // Settings
  $("#btnSaveSettings").addEventListener("click", function () {
    state.settings.notify = $("#settingNotify").checked;
    state.settings.sound = $("#settingSound").checked;
    state.settings.autoSave = $("#settingAutoSave").checked;

    var enabled = [];
    if (state.settings.notify) enabled.push("notifications");
    if (state.settings.sound) enabled.push("sounds");
    if (state.settings.autoSave) enabled.push("auto-save");

    if (enabled.length === 0) {
      toast("All settings disabled", "warn");
    } else {
      toast("Saved: " + enabled.join(", "), "success");
    }
    log("Settings saved: " + JSON.stringify(state.settings));
  });

  // ─── Search & Filter ───
  const items = [
    { name: "Apple", cat: "fruit" },
    { name: "Banana", cat: "fruit" },
    { name: "Cherry", cat: "fruit" },
    { name: "Carrot", cat: "veggie" },
    { name: "Broccoli", cat: "veggie" },
    { name: "Spinach", cat: "veggie" },
    { name: "Rice", cat: "grain" },
    { name: "Wheat", cat: "grain" },
    { name: "Oats", cat: "grain" },
  ];

  function renderSearch() {
    var query = $("#searchInput").value.toLowerCase();
    var filter = state.searchFilter;
    var results = items.filter(function (item) {
      var matchCat = filter === "all" || item.cat === filter;
      var matchQuery = !query || item.name.toLowerCase().indexOf(query) !== -1;
      return matchCat && matchQuery;
    });

    var list = $("#searchResults");
    list.innerHTML = "";
    results.forEach(function (item) {
      var li = document.createElement("li");
      var span = document.createElement("span");
      span.textContent = item.name;
      span.style.flex = "1";
      var tag = document.createElement("span");
      tag.className =
        "tag tag-" +
        (item.cat === "fruit" ? "high" : item.cat === "veggie" ? "med" : "low");
      tag.textContent = item.cat;
      li.appendChild(span);
      li.appendChild(tag);
      list.appendChild(li);
    });

    if (results.length === 0) {
      $("#searchCount").textContent = "No matches found";
    } else {
      $("#searchCount").textContent =
        "Showing " + results.length + " of " + items.length + " items";
    }
  }

  $("#searchInput").addEventListener("input", function () {
    renderSearch();
    log('Search: "' + this.value + '"');
  });

  $$("[data-filter]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      $$("[data-filter]").forEach(function (b) {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      state.searchFilter = btn.dataset.filter;
      renderSearch();
      log("Filter: " + btn.dataset.filter);
    });
  });

  renderSearch();

  // ─── Calculator ───
  $("#btnCalc").addEventListener("click", function () {
    var a = parseFloat($("#calcA").value);
    var b = parseFloat($("#calcB").value);
    var op = $("#calcOp").value;
    var result;

    if (isNaN(a) || isNaN(b)) {
      $("#calcResult").textContent = "Please enter valid numbers";
      toast("Invalid calculator input", "error");
      return;
    }

    switch (op) {
      case "add":
        result = a + b;
        break;
      case "sub":
        result = a - b;
        break;
      case "mul":
        result = a * b;
        break;
      case "div":
        if (b === 0) {
          $("#calcResult").textContent = "Cannot divide by zero";
          toast("Division by zero", "error");
          log("Calc: division by zero");
          return;
        }
        result = a / b;
        break;
      case "mod":
        if (b === 0) {
          $("#calcResult").textContent = "Cannot modulo by zero";
          toast("Modulo by zero", "error");
          log("Calc: modulo by zero");
          return;
        }
        result = a % b;
        break;
      case "pow":
        result = Math.pow(a, b);
        break;
      default:
        result = 0;
    }

    var rounded = Math.round(result * 10000) / 10000;
    $("#calcResult").textContent = "Result: " + rounded;
    toast(a + " " + op + " " + b + " = " + rounded, "success");
    log("Calc: " + a + " " + op + " " + b + " = " + rounded);
  });

  // ─── Drop Zone ───
  var dropZone = $("#dropZone");

  dropZone.addEventListener("dragover", function (e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    dropZone.classList.add("over");
  });

  dropZone.addEventListener("dragleave", function () {
    dropZone.classList.remove("over");
  });

  dropZone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropZone.classList.remove("over");
    var taskId = parseInt(e.dataTransfer.getData("text/plain"));
    if (taskId) {
      var task = state.tasks.find(function (t) {
        return t.id === taskId;
      });
      if (task) {
        state.tasks = state.tasks.filter(function (t) {
          return t.id !== taskId;
        });
        state.dropCount++;
        renderTasks();
        $("#dropLog").textContent =
          state.dropCount + ' item(s) archived. Last: "' + task.text + '"';
        toast("Archived: " + task.text, "info");
        log("Dropped task: " + task.text);
      } else {
        log("Drop: task not found");
      }
    } else {
      state.dropCount++;
      $("#dropLog").textContent = state.dropCount + " item(s) received.";
      log("Drop: non-task item");
    }
  });

  dropZone.addEventListener("click", function () {
    state.dropCount++;
    $("#dropLog").textContent = "Simulated upload #" + state.dropCount;
    toast("Simulated file upload", "info");
    log("Simulated upload");
  });

  // ─── Theme Toggle ───
  $("#btnThemeToggle").addEventListener("click", function () {
    if (state.theme === "dark") {
      state.theme = "light";
      document.documentElement.style.setProperty("--bg", "#f0f0f5");
      document.documentElement.style.setProperty("--surface", "#ffffff");
      document.documentElement.style.setProperty("--surface2", "#d0d5e0");
      document.documentElement.style.setProperty("--text", "#1a1a2e");
      document.documentElement.style.setProperty("--text-dim", "#555");
      this.textContent = "☀️ Theme";
      toast("Light theme activated", "info");
    } else {
      state.theme = "dark";
      document.documentElement.style.setProperty("--bg", "#1a1a2e");
      document.documentElement.style.setProperty("--surface", "#16213e");
      document.documentElement.style.setProperty("--surface2", "#0f3460");
      document.documentElement.style.setProperty("--text", "#eaeaea");
      document.documentElement.style.setProperty("--text-dim", "#8892a4");
      this.textContent = "🌙 Theme";
      toast("Dark theme activated", "info");
    }
    log("Theme: " + state.theme);
  });

  // ─── Reset All ───
  $("#btnResetAll").addEventListener("click", function () {
    showModal(
      "Reset Everything",
      "This will reset all widgets to their initial state. Continue?",
    ).then(function (confirmed) {
      if (confirmed) {
        state.counter = 0;
        state.tasks = [];
        state.taskIdSeq = 0;
        state.dropCount = 0;
        state.searchFilter = "all";
        updateCounter();
        renderTasks();
        renderSearch();
        $("#formName").value = "";
        $("#formEmail").value = "";
        $("#formAge").value = "";
        $("#formCategory").value = "";
        $("#formAgree").checked = false;
        $("#formErrors").textContent = "";
        $("#progressSlider").value = 0;
        $("#progressFill").style.width = "0%";
        $("#progressLabel").textContent = "0%";
        $("#calcA").value = "";
        $("#calcB").value = "";
        $("#calcResult").textContent = "Enter values and pick an operation";
        $("#dropLog").textContent = "No items dropped yet.";
        $$("[data-filter]").forEach(function (b) {
          b.classList.remove("active");
        });
        $('[data-filter="all"]').classList.add("active");
        $("#searchInput").value = "";
        updateStatus("All widgets reset");
        toast("Everything reset", "success");
        log("Full reset performed");
      } else {
        toast("Reset cancelled", "info");
      }
    });
  });

  // ─── Log Controls ───
  $("#btnClearLog").addEventListener("click", function () {
    state.logEntries = [];
    $("#eventLog").textContent = "— log cleared —";
    toast("Log cleared", "info");
  });

  $("#btnExportLog").addEventListener("click", function () {
    if (state.logEntries.length === 0) {
      toast("Nothing to export", "warn");
    } else {
      var blob = new Blob([state.logEntries.join("\n")], {
        type: "text/plain",
      });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = "event-log.txt";
      a.click();
      URL.revokeObjectURL(url);
      toast(
        "Log exported (" + state.logEntries.length + " entries)",
        "success",
      );
      log("Log exported");
    }
  });

  // ─── Keyboard Shortcuts ───
  document.addEventListener("keydown", function (e) {
    // Arrow keys for counter
    if (
      e.target.tagName !== "INPUT" &&
      e.target.tagName !== "TEXTAREA" &&
      e.target.tagName !== "SELECT"
    ) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        state.counter++;
        updateCounter();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        state.counter--;
        updateCounter();
      }
    }

    // Ctrl+L clear log
    if (e.ctrlKey && e.key === "l") {
      e.preventDefault();
      state.logEntries = [];
      $("#eventLog").textContent = "— log cleared (Ctrl+L) —";
      toast("Log cleared via shortcut", "info");
    }

    // Ctrl+E export log
    if (e.ctrlKey && e.key === "e") {
      e.preventDefault();
      $("#btnExportLog").click();
    }

    // Escape closes modal
    if (e.key === "Escape") {
      if ($("#modalOverlay").classList.contains("open")) {
        closeModal(false);
      }
    }
  });

  // ─── Init ───
  updateStatus("Ready — " + items.length + " searchable items loaded");
  log("Application initialized");
})();
