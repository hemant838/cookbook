/**
 * Emotion Analyzer — Smallest AI Pulse STT
 * Builds DOM, loads Chart.js, handles upload/analysis/visualization.
 */
(function () {
  'use strict';

  // Load CSS + set page meta
  document.title = 'Emotion Analyzer \u2014 Smallest AI';
  var link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'style.css';
  document.head.appendChild(link);

  if (!document.querySelector('meta[name="viewport"]')) {
    var meta = document.createElement('meta');
    meta.name = 'viewport';
    meta.content = 'width=device-width, initial-scale=1.0';
    document.head.appendChild(meta);
  }

  // Build DOM
  var root = document.createElement('div');
  root.className = 'container';
  root.innerHTML =
    '<div class="header">' +
      '<span class="badge">Pulse STT</span>' +
      '<h1>Emotion Analyzer</h1>' +
      '<p>Upload a recording. See who said what, and how they felt saying it. Powered by Smallest AI emotion detection.</p>' +
    '</div>' +
    '<div class="upload-area" id="uploadArea">' +
      '<span class="icon">\uD83C\uDFA4</span>' +
      '<h3>Drop your audio file here</h3>' +
      '<p>WAV, MP3, FLAC, OGG, M4A \u2014 up to 100 MB</p>' +
      '<input type="file" id="fileInput" accept="audio/*">' +
      '<div class="file-info" id="fileInfo">' +
        '<span class="name" id="fileName"></span>' +
        '<button class="remove" id="removeFile">Remove</button>' +
      '</div>' +
    '</div>' +
    '<button class="analyze-btn" id="analyzeBtn" disabled>Analyze Emotions</button>' +
    '<div class="error-msg" id="errorMsg"></div>' +
    '<div class="progress-section" id="progressSection">' +
      '<div class="step" id="progressStep">Uploading audio...</div>' +
      '<div class="progress-bar-bg"><div class="progress-bar-fill" id="progressBar"></div></div>' +
      '<div class="detail" id="progressDetail">This may take a minute for longer recordings</div>' +
    '</div>' +
    '<div class="results-section" id="resultsSection">' +
      '<div class="filters">' +
        '<div class="filter-group"><label>Emotions</label><div class="filter-chips" id="emotionFilters"></div></div>' +
        '<div class="filter-group"><label>Speakers</label><div class="filter-chips" id="speakerFilters"></div></div>' +
      '</div>' +
      '<div class="chart-card"><h3>Emotion Timeline</h3><div class="chart-wrapper"><canvas id="emotionChart"></canvas></div></div>' +
      '<div class="transcript-card"><h3>Transcript with Emotions</h3>' +
        '<table class="transcript-table"><thead><tr><th>Speaker</th><th>Time</th><th>Text</th><th>Emotions</th></tr></thead><tbody id="transcriptBody"></tbody></table>' +
      '</div>' +
    '</div>';
  document.body.appendChild(root);

  // Load Chart.js then boot
  var script = document.createElement('script');
  script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js';
  script.onload = function () { boot(true); };
  script.onerror = function () { console.error('Chart.js failed to load'); boot(false); };
  document.head.appendChild(script);

  function boot(chartAvailable) {

    // Config
    var EMOTION_KEYS = ['happiness', 'sadness', 'anger', 'fear', 'disgust'];
    var EMOTION_COLORS = { happiness: '#4ade80', sadness: '#60a5fa', anger: '#f87171', fear: '#c084fc', disgust: '#facc15' };
    var MAX_DEFAULT_SPEAKERS = 5;
    var SPEAKER_DASHES = [[], [6,3], [2,2], [8,4,2,4], [4,4], [12,4], [2,6], [6,3,2,3]];
    var EMOTION_ALIASES = {
      happy: 'happiness', joy: 'happiness', sad: 'sadness', sorrow: 'sadness',
      angry: 'anger', mad: 'anger', afraid: 'fear', scared: 'fear', disgusted: 'disgust', neutral: 'neutral',
    };

    // State
    var selectedFile = null;
    var analysisData = null;
    var chart = null;
    var activeEmotions = new Set(EMOTION_KEYS);
    var activeSpeakers = new Set();

    // DOM refs
    var $upload     = document.getElementById('uploadArea');
    var $fileInput  = document.getElementById('fileInput');
    var $fileInfo   = document.getElementById('fileInfo');
    var $fileName   = document.getElementById('fileName');
    var $removeBtn  = document.getElementById('removeFile');
    var $analyzeBtn = document.getElementById('analyzeBtn');
    var $error      = document.getElementById('errorMsg');
    var $progress   = document.getElementById('progressSection');
    var $progBar    = document.getElementById('progressBar');
    var $progStep   = document.getElementById('progressStep');
    var $progDetail = document.getElementById('progressDetail');
    var $results    = document.getElementById('resultsSection');

    // Helpers
    function fmtSpeaker(s) {
      if (s == null || s === '') return 'Unknown';
      if (typeof s === 'string' && s.startsWith('Speaker ')) return s;
      if (typeof s === 'string' && s.startsWith('speaker_')) return s.replace('speaker_', 'Speaker ');
      if (typeof s === 'number') return 'Speaker ' + s;
      return String(s);
    }

    function fmtTime(sec) {
      var m = Math.floor(sec / 60);
      var s = Math.floor(sec % 60);
      var ms = Math.round((sec % 1) * 10);
      return m + ':' + String(s).padStart(2, '0') + '.' + ms;
    }

    function showProgress(step, pct, detail) {
      $progress.classList.add('visible');
      $progStep.textContent = step;
      $progDetail.textContent = detail;
      $progBar.style.width = pct + '%';
    }
    function hideProgress() { $progress.classList.remove('visible'); }
    function setProgress(pct) { $progBar.style.width = pct + '%'; }
    function showError(msg) { $error.textContent = msg; $error.classList.add('visible'); }
    function hideError() { $error.classList.remove('visible'); }

    // Normalize emotion keys from API response
    function normalizeEmotionKeys(segments) {
      segments.forEach(function (seg) {
        if (!seg.emotions) return;
        var normalized = {};
        Object.keys(seg.emotions).forEach(function (key) {
          normalized[EMOTION_ALIASES[key] || key] = seg.emotions[key];
        });
        seg.emotions = normalized;
        Object.keys(normalized).forEach(function (k) {
          if (EMOTION_KEYS.indexOf(k) === -1 && k !== 'neutral') {
            EMOTION_KEYS.push(k);
            EMOTION_COLORS[k] = '#a3a3a3';
          }
        });
      });
    }

    // Chart
    function chartOptions() {
      return {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 600 },
        interaction: { mode: 'nearest', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#0e2e33', titleColor: '#F8FAF5', bodyColor: '#9cbcbc',
            borderColor: '#1a4448', borderWidth: 1, padding: 12,
            callbacks: {
              title: function (items) { return 'Time: ' + fmtTime(items[0].parsed.x); },
              label: function (item) { return ' ' + item.dataset.label + ': ' + (item.parsed.y * 100).toFixed(0) + '%'; }
            }
          }
        },
        scales: {
          x: {
            type: 'linear',
            title: { display: true, text: 'Time (s)', color: '#6a9999', font: { size: 11 } },
            ticks: { color: '#6a9999', font: { size: 10 }, callback: function (v) { return fmtTime(v); } },
            grid: { color: 'rgba(26,68,72,0.4)' }
          },
          y: {
            min: 0, max: 1,
            title: { display: true, text: 'Score', color: '#6a9999', font: { size: 11 } },
            ticks: { color: '#6a9999', font: { size: 10 }, callback: function (v) { return (v * 100) + '%'; } },
            grid: { color: 'rgba(26,68,72,0.4)' }
          }
        }
      };
    }

    function initEmptyChart() {
      if (!chartAvailable) return;
      $results.classList.add('visible');
      renderEmotionChips();
      document.getElementById('speakerFilters').innerHTML =
        '<span style="color:var(--text-muted);font-size:0.8rem">Upload audio to see speakers</span>';
      chart = new Chart(document.getElementById('emotionChart').getContext('2d'), {
        type: 'line', data: { datasets: [] }, options: chartOptions()
      });
    }

    function buildChartData() {
      if (!analysisData || !analysisData.segments) return { datasets: [] };
      var allSpeakers = analysisData.speakers || [];
      var segments = analysisData.segments.filter(function (s) { return s.emotions && !s.skipped; });
      var datasets = [];

      EMOTION_KEYS.forEach(function (em) {
        if (!activeEmotions.has(em)) return;
        allSpeakers.forEach(function (sp) {
          if (!activeSpeakers.has(sp)) return;
          var spIdx = allSpeakers.indexOf(sp);
          var points = segments
            .filter(function (s) { return s.speaker === sp; })
            .map(function (s) { return { x: s.start, y: (s.emotions[em] != null) ? s.emotions[em] : 0 }; })
            .sort(function (a, b) { return a.x - b.x; });
          if (!points.length) return;
          datasets.push({
            label: fmtSpeaker(sp) + ' \u2014 ' + em.charAt(0).toUpperCase() + em.slice(1),
            data: points,
            borderColor: EMOTION_COLORS[em] || '#a3a3a3',
            backgroundColor: (EMOTION_COLORS[em] || '#a3a3a3') + '22',
            borderWidth: 2, borderDash: SPEAKER_DASHES[spIdx % SPEAKER_DASHES.length],
            pointRadius: 4, pointHoverRadius: 7, tension: 0.3, fill: false,
          });
        });
      });
      return { datasets: datasets };
    }

    function clearChartData() { if (chart) { chart.data = { datasets: [] }; chart.update(); } }
    function updateChartData() { if (!chart) return; chart.data = buildChartData(); chart.update(); }

    // Filter chips
    function renderEmotionChips() {
      var container = document.getElementById('emotionFilters');
      container.innerHTML = '';
      EMOTION_KEYS.forEach(function (em) {
        var btn = document.createElement('button');
        btn.className = 'chip emotion-' + em + (activeEmotions.has(em) ? ' active' : '');
        btn.textContent = em.charAt(0).toUpperCase() + em.slice(1);
        btn.onclick = function () {
          if (activeEmotions.has(em)) activeEmotions.delete(em); else activeEmotions.add(em);
          btn.classList.toggle('active');
          updateChartData();
        };
        container.appendChild(btn);
      });
    }

    function speakerDashSVG(spIdx) {
      var dash = SPEAKER_DASHES[spIdx % SPEAKER_DASHES.length];
      var attr = dash.length ? ' stroke-dasharray="' + dash.join(',') + '"' : '';
      return '<svg class="dash-icon" width="22" height="10" viewBox="0 0 22 10">' +
             '<line x1="0" y1="5" x2="22" y2="5" stroke="currentColor" stroke-width="2.5"' + attr + '/></svg>';
    }

    function renderSpeakerChips(speakers) {
      var container = document.getElementById('speakerFilters');
      container.innerHTML = '';
      speakers.forEach(function (sp, idx) {
        var btn = document.createElement('button');
        btn.className = 'chip speaker-chip' + (activeSpeakers.has(sp) ? ' active' : '');
        btn.innerHTML = speakerDashSVG(idx) + ' ' + fmtSpeaker(sp);
        btn.onclick = function () {
          if (activeSpeakers.has(sp)) activeSpeakers.delete(sp); else activeSpeakers.add(sp);
          btn.classList.toggle('active');
          updateChartData();
        };
        container.appendChild(btn);
      });
    }

    // Transcript
    function clearTranscript() { document.getElementById('transcriptBody').innerHTML = ''; }

    function renderTranscript(segments) {
      var tbody = document.getElementById('transcriptBody');
      tbody.innerHTML = '';
      segments.forEach(function (seg) {
        var tr = document.createElement('tr');

        var tdSp = document.createElement('td');
        tdSp.innerHTML = '<span class="speaker-tag">' + fmtSpeaker(seg.speaker) + '</span>';

        var tdTime = document.createElement('td');
        tdTime.className = 'time';
        tdTime.textContent = fmtTime(seg.start) + ' \u2013 ' + fmtTime(seg.end);

        var tdText = document.createElement('td');
        tdText.className = 'text';
        tdText.textContent = seg.text;

        var tdEm = document.createElement('td');
        if (seg.emotions && !seg.skipped) {
          var group = document.createElement('div');
          group.className = 'emotion-bar-group';
          Object.entries(seg.emotions)
            .sort(function (a, b) { return b[1] - a[1]; })
            .slice(0, 3)
            .forEach(function (pair) {
              var pill = document.createElement('span');
              pill.className = 'emotion-mini';
              pill.innerHTML = '<span class="dot" style="background:' + (EMOTION_COLORS[pair[0]] || '#999') + '"></span>' +
                               pair[0] + ' ' + (pair[1] * 100).toFixed(0) + '%';
              group.appendChild(pill);
            });
          tdEm.appendChild(group);
        } else {
          tdEm.innerHTML = '<span style="color:var(--text-muted);font-size:0.75rem">\u2014</span>';
        }

        tr.appendChild(tdSp); tr.appendChild(tdTime); tr.appendChild(tdText); tr.appendChild(tdEm);
        tbody.appendChild(tr);
      });
    }

    // Populate results
    function populateResults(data) {
      activeSpeakers = new Set((data.speakers || []).slice(0, MAX_DEFAULT_SPEAKERS));
      activeEmotions = new Set(EMOTION_KEYS);
      renderEmotionChips();
      renderSpeakerChips(data.speakers || []);
      updateChartData();
      renderTranscript(data.segments || []);
      $analyzeBtn.disabled = false;
    }

    // Upload handling
    $upload.addEventListener('click', function () { $fileInput.click(); });
    $upload.addEventListener('dragover', function (e) { e.preventDefault(); $upload.classList.add('dragover'); });
    $upload.addEventListener('dragleave', function () { $upload.classList.remove('dragover'); });
    $upload.addEventListener('drop', function (e) {
      e.preventDefault(); $upload.classList.remove('dragover');
      if (e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
    });
    $fileInput.addEventListener('change', function () { if ($fileInput.files.length) pickFile($fileInput.files[0]); });
    $removeBtn.addEventListener('click', function (e) { e.stopPropagation(); clearFile(); });

    function pickFile(file) {
      selectedFile = file;
      $fileName.textContent = file.name + ' (' + (file.size / 1024 / 1024).toFixed(1) + ' MB)';
      $fileInfo.classList.add('visible');
      $analyzeBtn.disabled = false;
      hideError();
    }

    function clearFile() {
      selectedFile = null;
      $fileInput.value = '';
      $fileInfo.classList.remove('visible');
      $analyzeBtn.disabled = true;
    }

    // Analyze
    $analyzeBtn.addEventListener('click', runAnalysis);

    async function runAnalysis() {
      if (!selectedFile) return;
      hideError();
      $analyzeBtn.disabled = true;
      analysisData = null;
      clearChartData();
      clearTranscript();

      showProgress('Uploading & transcribing audio...', 10, 'Diarizing speakers and extracting timestamps');

      var formData = new FormData();
      formData.append('audio', selectedFile);

      var progress = 10;
      var tick = setInterval(function () {
        progress = Math.min(progress + 2, 85);
        setProgress(progress);
        if (progress > 40) {
          $progStep.textContent = 'Analyzing emotions per speaker segment...';
          $progDetail.textContent = 'Running emotion detection on each utterance';
        }
      }, 1500);

      try {
        var resp = await fetch('/api/analyze', { method: 'POST', body: formData });
        clearInterval(tick);
        var data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Server error (' + resp.status + ')');

        setProgress(95);
        $progStep.textContent = 'Rendering results...';
        $progDetail.textContent = '';

        analysisData = data;
        normalizeEmotionKeys(analysisData.segments);
        setProgress(100);

        setTimeout(function () { hideProgress(); populateResults(analysisData); }, 300);
      } catch (err) {
        clearInterval(tick);
        hideProgress();
        showError(err.message);
        $analyzeBtn.disabled = false;
        console.error('Analysis failed:', err);
      }
    }

    initEmptyChart();
  }

})();
