(() => {
  "use strict";

  const config = window.AUTOCLIP_CONFIG || { pollIntervalMs: 1500 };

  const form = document.getElementById("clip-form");
  const urlInput = document.getElementById("url-input");
  const lengthSelect = document.getElementById("clip-length");
  const submitBtn = document.getElementById("submit-btn");
  const formError = document.getElementById("form-error");
  const progressSection = document.getElementById("progress-section");
  const progressMessage = document.getElementById("progress-message");
  const progressPercent = document.getElementById("progress-percent");
  const progressBar = document.getElementById("progress-bar");
  const resultsSection = document.getElementById("results-section");
  const resultsTitle = document.getElementById("results-title");
  const resultsGrid = document.getElementById("results-grid");

  // Validasi sisi klien (validasi utama tetap dilakukan di server)
  const YOUTUBE_RE = /^(https?:\/\/)?((www|m|music)\.)?(youtube\.com|youtu\.be)\/.+/i;

  let pollTimer = null;
  let failedPolls = 0;

  function showError(message) {
    formError.textContent = message;
    formError.hidden = false;
  }

  function clearError() {
    formError.textContent = "";
    formError.hidden = true;
  }

  function setBusy(busy) {
    submitBtn.disabled = busy;
    submitBtn.textContent = busy ? "Memproses..." : "Buat Klip";
  }

  function updateProgress(percent, message) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    progressBar.style.width = value + "%";
    progressPercent.textContent = value + "%";
    if (message) {
      progressMessage.textContent = message;
    }
  }

  function renderClips(title, clips) {
    resultsGrid.textContent = "";
    resultsTitle.textContent = title ? "Hasil Klip: " + title : "Hasil Klip";

    clips.forEach((clip) => {
      const card = document.createElement("article");
      card.className = "clip-card";

      const video = document.createElement("video");
      video.controls = true;
      video.preload = "metadata";
      video.src = clip.preview_url;

      const body = document.createElement("div");
      body.className = "clip-body";

      const name = document.createElement("p");
      name.className = "clip-title";
      name.textContent = "Klip " + clip.index;

      const meta = document.createElement("p");
      meta.className = "clip-meta";
      meta.textContent = clip.start_label + " - " + clip.end_label + " (" + clip.duration + " dtk)";

      const link = document.createElement("a");
      link.className = "btn";
      link.href = clip.download_url;
      link.textContent = "Unduh Klip";

      body.append(name, meta, link);
      card.append(video, body);
      resultsGrid.append(card);
    });

    resultsSection.hidden = false;
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  async function pollStatus(jobId) {
    try {
      const response = await fetch("/api/status/" + encodeURIComponent(jobId));
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Gagal membaca status.");
      }
      failedPolls = 0;
      updateProgress(data.progress, data.message);

      if (data.status === "done") {
        setBusy(false);
        renderClips(data.title, data.clips || []);
        return;
      }
      if (data.status === "error") {
        setBusy(false);
        progressSection.hidden = true;
        showError(data.error || "Terjadi kesalahan saat memproses video.");
        return;
      }
    } catch (err) {
      failedPolls += 1;
      if (failedPolls >= 5) {
        setBusy(false);
        progressSection.hidden = true;
        showError("Koneksi ke server terputus. Muat ulang halaman lalu coba lagi.");
        return;
      }
    }
    pollTimer = setTimeout(() => pollStatus(jobId), config.pollIntervalMs);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();
    stopPolling();
    resultsSection.hidden = true;

    const url = urlInput.value.trim();
    if (!YOUTUBE_RE.test(url)) {
      showError("URL tidak valid. Masukkan link video YouTube, contoh: https://www.youtube.com/watch?v=...");
      return;
    }

    setBusy(true);
    failedPolls = 0;
    progressSection.hidden = false;
    updateProgress(0, "Mengirim permintaan...");

    try {
      const response = await fetch("/api/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, clip_seconds: Number(lengthSelect.value) }),
      });
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Permintaan ditolak server.");
      }
      pollStatus(data.job_id);
    } catch (err) {
      setBusy(false);
      progressSection.hidden = true;
      showError(err.message || "Gagal menghubungi server.");
    }
  });
})();
