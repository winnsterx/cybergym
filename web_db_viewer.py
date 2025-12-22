#!/usr/bin/env python3
"""
Web-based SQLite database viewer for CyberGym submissions.
Runs a local web server to browse the database.
Auto-refreshes when database changes are detected.

Supports both local SQLite database and Modal server API.

Usage:
    # Local mode (default)
    python web_db_viewer.py --db-path server_poc/poc.db

    # Modal mode
    python web_db_viewer.py --modal --server-url https://your-modal-server.modal.run --api-key your-api-key
"""
import argparse
import sqlite3
import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import json
import html
import urllib.request
import urllib.error


# Global configuration (set by CLI args)
CONFIG = {
    "mode": "local",  # "local" or "modal"
    "db_path": Path("server_poc/poc.db"),
    "server_url": "",
    "api_key": "",
    "transcript_dir": None,  # Path to transcript directory with summary.json
}

# Transcript metrics cache: agent_id -> {tokens, duration, ...}
TRANSCRIPT_METRICS = {}

# Track database modification time for auto-refresh (local mode only)
db_mtime = None


def load_transcript_metrics(transcript_dir: Path) -> dict:
    """
    Load metrics from transcript directory.

    Returns a dict mapping agent_id -> {
        "total_tokens": int,
        "duration_seconds": float,
        "task_id": str,
        "run_number": int,
        "correct": bool,
    }
    """
    metrics = {}

    if not transcript_dir or not transcript_dir.exists():
        return metrics

    # Load summary.json for token counts and timing
    summary_path = transcript_dir / "summary.json"
    if not summary_path.exists():
        print(f"[transcript] summary.json not found at {summary_path}")
        return metrics

    try:
        with open(summary_path) as f:
            summary = json.load(f)
    except Exception as e:
        print(f"[transcript] Error loading summary.json: {e}")
        return metrics

    # Build mapping from (task_id, run_number) -> metrics from summary
    task_run_metrics = {}
    for task_id, task_data in summary.get("tasks", {}).items():
        for run_result in task_data.get("run_results", []):
            run_number = run_result.get("run_id", 0)
            telemetry = run_result.get("telemetry", {})
            tokens = telemetry.get("tokens", {})
            timing = telemetry.get("timing", {})

            task_run_metrics[(task_id, run_number)] = {
                "total_tokens": tokens.get("total_tokens", 0),
                "prompt_tokens": tokens.get("prompt_tokens", 0),
                "completion_tokens": tokens.get("completion_tokens", 0),
                "duration_seconds": timing.get("duration_seconds", 0),
                "correct": run_result.get("correct", False),
            }

    # Load metadata.json files to get agent_id for each (task_id, run_number)
    runs_dir = transcript_dir / "runs"
    if runs_dir.exists():
        for task_dir in runs_dir.iterdir():
            if not task_dir.is_dir():
                continue
            # Convert directory name back to task_id (google-ctf_task -> google-ctf:task)
            task_id = task_dir.name.replace("_", ":", 1)

            for run_dir in task_dir.iterdir():
                if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
                    continue

                try:
                    run_number = int(run_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                metadata_path = run_dir / "agent" / "metadata.json"
                if not metadata_path.exists():
                    continue

                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    agent_id = metadata.get("agent_id")
                    if agent_id and (task_id, run_number) in task_run_metrics:
                        metrics[agent_id] = {
                            **task_run_metrics[(task_id, run_number)],
                            "task_id": task_id,
                            "run_number": run_number,
                        }
                except Exception as e:
                    print(f"[transcript] Error loading {metadata_path}: {e}")

    print(f"[transcript] Loaded metrics for {len(metrics)} agents")
    return metrics


def get_db_mtime():
    """Get the last modification time of the database (local mode only)."""
    if CONFIG["mode"] == "local" and CONFIG["db_path"].exists():
        return os.path.getmtime(CONFIG["db_path"])
    return None


def modal_api_request(endpoint: str, payload: dict | None = None) -> dict | list:
    """Make an API request to the Modal server."""
    url = f"{CONFIG['server_url']}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": CONFIG["api_key"],
    }

    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []  # No data found
        raise


class DBViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self.serve_index()
        elif path == "/api/tables":
            self.serve_tables()
        elif path == "/api/submissions":
            self.serve_submissions(query)
        elif path == "/api/submission":
            submission_id = query.get("id", [None])[0]
            if submission_id:
                self.serve_submission_detail(submission_id)
            else:
                self.send_error(400, "Missing submission ID")
        elif path == "/api/db-mtime":
            self.serve_db_mtime()
        elif path == "/api/mode":
            self.serve_mode_info()
        else:
            self.send_error(404)

    def serve_index(self):
        """Serve the main HTML page."""
        html_content = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CyberGym Database Viewer</title>
    <style>
        @keyframes slideIn {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        @keyframes slideOut {
            from {
                transform: translateX(0);
                opacity: 1;
            }
            to {
                transform: translateX(400px);
                opacity: 0;
            }
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background: #f5f5f5;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            border-bottom: 2px solid #4CAF50;
            padding-bottom: 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .auto-refresh-indicator {
            font-size: 0.7em;
            color: #4CAF50;
            font-weight: normal;
        }
        .auto-refresh-indicator::before {
            content: '●';
            margin-right: 5px;
            animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background: #4CAF50;
            color: white;
            font-weight: 600;
        }
        tr:hover {
            background: #f5f5f5;
            cursor: pointer;
        }
        .submission-detail {
            margin-top: 20px;
            padding: 20px;
            background: #f9f9f9;
            border-radius: 4px;
            display: none;
        }
        .submission-detail.active {
            display: block;
        }
        .score {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: bold;
        }
        .score.high {
            background: #4CAF50;
            color: white;
        }
        .score.medium {
            background: #FF9800;
            color: white;
        }
        .score.low {
            background: #f44336;
            color: white;
        }
        pre {
            background: #282c34;
            color: #abb2bf;
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .back-btn {
            background: #4CAF50;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            margin-bottom: 10px;
        }
        .back-btn:hover {
            background: #45a049;
        }
        .metadata {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .metadata-item {
            padding: 10px;
            background: white;
            border-radius: 4px;
            border-left: 3px solid #4CAF50;
        }
        .metadata-label {
            font-weight: 600;
            color: #666;
            font-size: 0.9em;
        }
        .metadata-value {
            color: #333;
            margin-top: 5px;
        }
        .loading {
            text-align: center;
            padding: 20px;
            color: #666;
        }
        .filter {
            margin: 20px 0;
            padding: 15px;
            background: #f9f9f9;
            border-radius: 4px;
        }
        .filter input, .filter select {
            padding: 8px;
            margin-right: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        h4 {
            color: #555;
            margin-top: 15px;
            margin-bottom: 10px;
        }
        ul {
            list-style: none;
            padding-left: 0;
        }
        ul li {
            padding: 5px 0;
            border-bottom: 1px solid #f0f0f0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <span>CyberGym Database Viewer</span>
            <span id="mode-indicator" class="auto-refresh-indicator">Loading...</span>
        </h1>

        <div id="list-view">
            <div class="filter">
                <select id="submission-type">
                    <option value="re">RE Submissions (all)</option>
                    <option value="re-five-point">RE Submissions (five-point)</option>
                    <option value="re-granular">RE Submissions (granular)</option>
                    <option value="re-typecast_issues">RE (typecast_issues)</option>
                    <option value="re-incorrect_return_behavior">RE (incorrect_return_behavior)</option>
                    <option value="re-struct_class_recovery">RE (struct_class_recovery)</option>
                    <option value="re-function_signature_recovery">RE (function_signature_recovery)</option>
                    <option value="ctf">CTF Submissions</option>
                </select>
                <input type="text" id="search-task" placeholder="Search by task ID...">
                <input type="text" id="search-agent" placeholder="Search by agent ID...">
            </div>
            <div class="loading">Loading submissions...</div>
            <table id="submissions-table" style="display: none;">
                <thead>
                    <tr>
                        <th>Task ID</th>
                        <th>Agent ID (first 8)</th>
                        <th>Schema</th>
                        <th>Scores</th>
                        <th>Created</th>
                        <th>Evaluated</th>
                    </tr>
                </thead>
                <tbody id="submissions-body"></tbody>
            </table>
        </div>

        <div id="detail-view" class="submission-detail">
            <button class="back-btn" onclick="showList()">← Back to List</button>
            <div id="detail-content"></div>
        </div>
    </div>

    <script>
        let allSubmissions = [];
        let lastDbMtime = null;
        let autoRefreshInterval = null;
        let currentMode = 'local';  // 'local' or 'modal'
        let currentSubmissionType = 're';  // 're', 're-five-point', 're-granular', or 'ctf'

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Single-criterion schemas that use 0-2 scale (same as granular)
        const SINGLE_CRITERION_SCHEMAS = [
            'typecast_issues', 'incorrect_return_behavior',
            'struct_class_recovery', 'function_signature_recovery'
        ];

        function getScoreClass(score, schema = 'five-point') {
            // Handle different scoring scales
            if (score === null || score === undefined) return '';
            if (schema === 'granular' || SINGLE_CRITERION_SCHEMAS.includes(schema)) {
                // Granular/single-criterion: 0-2 scale (0=bad, 1=ok, 2=good)
                if (score >= 1.5) return 'high';
                if (score <= 0.5) return 'low';
                return 'medium';
            } else {
                // Five-point: -2 to +2 scale
                if (score >= 1) return 'high';
                if (score <= -1) return 'low';
                return 'medium';
            }
        }

        function formatScore(score) {
            if (score === null || score === undefined) return 'N/A';
            return score.toFixed(2);
        }

        async function loadSubmissions(silent = false) {
            try {
                const url = `/api/submissions?type=${currentSubmissionType}`;
                const response = await fetch(url);
                allSubmissions = await response.json();
                displaySubmissions(allSubmissions);

                if (!silent) {
                    // Update the last known mtime after loading
                    const mtimeResponse = await fetch('/api/db-mtime');
                    const mtimeData = await mtimeResponse.json();
                    lastDbMtime = mtimeData.mtime;
                }
            } catch (error) {
                console.error('Error loading submissions:', error);
                if (!silent) {
                    document.querySelector('.loading').textContent = 'Error loading submissions: ' + error;
                }
            }
        }

        function onSubmissionTypeChange() {
            currentSubmissionType = document.getElementById('submission-type').value;
            document.querySelector('.loading').style.display = 'block';
            document.querySelector('.loading').textContent = 'Loading submissions...';
            document.getElementById('submissions-table').style.display = 'none';
            loadSubmissions();
        }

        async function checkForUpdates() {
            try {
                const response = await fetch('/api/db-mtime');
                const data = await response.json();

                if (lastDbMtime !== null && data.mtime !== lastDbMtime) {
                    console.log('Database updated, refreshing...');
                    lastDbMtime = data.mtime;
                    await loadSubmissions(true);

                    // Show a subtle notification
                    showNotification('Database updated');
                }
            } catch (error) {
                console.error('Error checking for updates:', error);
            }
        }

        function showNotification(message) {
            const notification = document.createElement('div');
            notification.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: #4CAF50;
                color: white;
                padding: 12px 20px;
                border-radius: 4px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                z-index: 1000;
                animation: slideIn 0.3s ease-out;
            `;
            notification.textContent = message;
            document.body.appendChild(notification);

            setTimeout(() => {
                notification.style.animation = 'slideOut 0.3s ease-out';
                setTimeout(() => notification.remove(), 300);
            }, 2000);
        }

        function startAutoRefresh() {
            // Check for updates every 2 seconds
            autoRefreshInterval = setInterval(checkForUpdates, 2000);
        }

        function stopAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
                autoRefreshInterval = null;
            }
        }

        function displaySubmissions(submissions) {
            const tbody = document.getElementById('submissions-body');
            const thead = document.querySelector('#submissions-table thead tr');
            tbody.innerHTML = '';

            // Update table headers based on submission type
            const isCtf = currentSubmissionType === 'ctf';
            const isRe = currentSubmissionType.startsWith('re');
            if (isCtf) {
                thead.innerHTML = `
                    <th>Task ID</th>
                    <th>Agent ID (first 8)</th>
                    <th>Flag Submitted</th>
                    <th>Result</th>
                    <th>Duration</th>
                    <th>Tokens</th>
                `;
            } else {
                thead.innerHTML = `
                    <th>Task ID</th>
                    <th>Agent ID (first 8)</th>
                    <th>Schema</th>
                    <th>Scores</th>
                    <th>Created</th>
                    <th>Evaluated</th>
                `;
            }

            submissions.forEach(sub => {
                const row = document.createElement('tr');

                if (isCtf) {
                    // CTF submission display
                    const resultClass = sub.correct === 1 ? 'high' : 'low';
                    const resultText = sub.correct === 1 ? 'Correct' : 'Incorrect';

                    // Format duration
                    let durationStr = 'N/A';
                    if (sub.duration_seconds) {
                        const mins = Math.floor(sub.duration_seconds / 60);
                        const secs = Math.round(sub.duration_seconds % 60);
                        durationStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
                    }

                    // Format tokens
                    let tokensStr = 'N/A';
                    if (sub.total_tokens) {
                        tokensStr = sub.total_tokens.toLocaleString();
                    }

                    // Truncate flag for display
                    const flagDisplay = sub.submitted_flag && sub.submitted_flag.length > 30
                        ? sub.submitted_flag.substring(0, 30) + '...'
                        : (sub.submitted_flag || 'N/A');

                    row.innerHTML = `
                        <td>${sub.task_id}</td>
                        <td><code>${sub.agent_id.substring(0, 8)}</code></td>
                        <td title="${sub.submitted_flag || 'N/A'}"><code>${flagDisplay}</code></td>
                        <td><span class="score ${resultClass}">${resultText}</span></td>
                        <td>${durationStr}</td>
                        <td>${tokensStr}</td>
                    `;
                } else {
                    // RE submission display
                    row.onclick = () => loadSubmissionDetail(sub.submission_id);

                    // Parse category scores
                    let scoresHTML = 'N/A';
                    const schema = sub.grading_schema || 'five-point';
                    if (sub.category_scores) {
                        try {
                            const scores = JSON.parse(sub.category_scores);
                            const entries = Object.entries(scores);
                            // For granular (25 categories), show avg score instead of all
                            if (schema === 'granular' && entries.length > 10) {
                                const avg = entries.reduce((sum, [_, s]) => sum + s, 0) / entries.length;
                                scoresHTML = `<span class="score ${getScoreClass(avg, schema)}">Avg: ${formatScore(avg)}</span> (${entries.length} criteria)`;
                            } else if (entries.length === 1) {
                                // Single-criterion: just show the score directly
                                const [cat, score] = entries[0];
                                scoresHTML = `<span class="score ${getScoreClass(score, schema)}">${formatScore(score)}</span>`;
                            } else {
                                const scoreEntries = entries.map(([cat, score]) => {
                                    const shortCat = cat.split('_').map(w => w[0].toUpperCase()).join('');
                                    return `<span class="score ${getScoreClass(score, schema)}">${shortCat}: ${formatScore(score)}</span>`;
                                }).join(' ');
                                scoresHTML = scoreEntries;
                            }
                        } catch (e) {
                            scoresHTML = 'Error';
                        }
                    }

                    row.innerHTML = `
                        <td>${sub.task_id}</td>
                        <td><code>${sub.agent_id.substring(0, 8)}</code></td>
                        <td>${sub.grading_schema || 'N/A'}</td>
                        <td>${scoresHTML}</td>
                        <td>${sub.created_at}</td>
                        <td>${sub.evaluated_at || 'Not evaluated'}</td>
                    `;
                }
                tbody.appendChild(row);
            });

            document.querySelector('.loading').style.display = 'none';
            document.getElementById('submissions-table').style.display = 'table';
        }

        async function loadSubmissionDetail(submissionId) {
            try {
                const response = await fetch(`/api/submission?id=${submissionId}`);
                const sub = await response.json();

                const detailContent = document.getElementById('detail-content');

                // Helper to format snake_case to Title Case
                const formatCriterion = (str) => str.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');

                // Statistical helper functions
                const median = (arr) => {
                    if (!arr.length) return null;
                    const sorted = [...arr].sort((a, b) => a - b);
                    const mid = Math.floor(sorted.length / 2);
                    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
                };

                const std = (arr) => {
                    if (arr.length < 2) return 0;
                    const mean = arr.reduce((a, b) => a + b, 0) / arr.length;
                    const squaredDiffs = arr.map(x => Math.pow(x - mean, 2));
                    return Math.sqrt(squaredDiffs.reduce((a, b) => a + b, 0) / arr.length);
                };

                const min = (arr) => arr.length ? Math.min(...arr) : null;
                const max = (arr) => arr.length ? Math.max(...arr) : null;

                const allEvaluations = sub.all_evaluations || [];
                const numJudges = allEvaluations.length;
                const schema = sub.grading_schema || 'five-point';

                // Helper to get score class using the submission's schema
                const getDetailScoreClass = (score) => getScoreClass(score, schema);

                // Compute score statistics across all judges
                let scoreStatsHTML = '';
                if (numJudges > 0) {
                    // Collect all category names from first evaluation
                    const firstEval = allEvaluations[0];
                    const categories = Object.keys(firstEval.category_scores || {});

                    // Compute stats for each category
                    const categoryStats = {};
                    for (const cat of categories) {
                        const scores = allEvaluations
                            .map(e => e.category_scores?.[cat])
                            .filter(s => s !== undefined && s !== null);
                        categoryStats[cat] = {
                            median: median(scores),
                            std: std(scores),
                            min: min(scores),
                            max: max(scores),
                            scores: scores
                        };
                    }

                    scoreStatsHTML = `<h3>Score Statistics (${numJudges} judge${numJudges > 1 ? 's' : ''})</h3>`;
                    scoreStatsHTML += `<table class="stats-table" style="width: 100%; margin-top: 10px;">
                        <thead>
                            <tr>
                                <th>Category</th>
                                <th>Median</th>
                                <th>Std Dev</th>
                                <th>Min</th>
                                <th>Max</th>
                            </tr>
                        </thead>
                        <tbody>`;

                    for (const [category, stats] of Object.entries(categoryStats)) {
                        scoreStatsHTML += `
                            <tr>
                                <td>${formatCriterion(category)}</td>
                                <td><span class="score ${getScoreClass(stats.median, schema)}">${formatScore(stats.median)}</span></td>
                                <td>${stats.std.toFixed(2)}</td>
                                <td><span class="score ${getScoreClass(stats.min, schema)}">${stats.min}</span></td>
                                <td><span class="score ${getScoreClass(stats.max, schema)}">${stats.max}</span></td>
                            </tr>
                        `;
                    }
                    scoreStatsHTML += '</tbody></table>';
                }

                // Build individual judge scores (toggleable)
                let judgeScoresHTML = '';
                if (numJudges > 0) {
                    judgeScoresHTML = `
                        <div class="judge-toggle" style="margin-top: 20px;">
                            <button class="back-btn" onclick="toggleJudgeDetails()" id="judge-toggle-btn">
                                Show Individual Judge Scores (${numJudges})
                            </button>
                        </div>
                        <div id="judge-details" style="display: none; margin-top: 15px;">
                    `;

                    allEvaluations.forEach((evalData, idx) => {
                        const judgeNum = evalData.judge_number || (idx + 1);
                        let detailedScores = evalData.detailed_scores;

                        // Parse detailed_scores if it's a string
                        if (typeof detailedScores === 'string') {
                            try {
                                detailedScores = JSON.parse(detailedScores);
                            } catch (e) {
                                detailedScores = null;
                            }
                        }

                        judgeScoresHTML += `
                            <div class="judge-section" style="border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin-bottom: 15px; background: #fafafa;">
                                <h4 style="margin-top: 0; color: #4CAF50;">Judge ${judgeNum}</h4>
                        `;

                        // Show category scores for this judge
                        if (evalData.category_scores) {
                            judgeScoresHTML += '<div class="metadata" style="margin-bottom: 15px;">';
                            for (const [cat, score] of Object.entries(evalData.category_scores)) {
                                judgeScoresHTML += `
                                    <div class="metadata-item">
                                        <div class="metadata-label">${formatCriterion(cat)}</div>
                                        <div class="metadata-value"><span class="score ${getScoreClass(score, schema)}">${score}</span></div>
                                    </div>
                                `;
                            }
                            judgeScoresHTML += '</div>';
                        }

                        // Show detailed reasoning for this judge
                        if (detailedScores && typeof detailedScores === 'object') {
                            for (const [category, categoryData] of Object.entries(detailedScores)) {
                                if (category === 'summary') continue;

                                if (typeof categoryData === 'object' && categoryData !== null) {
                                    // Flat format
                                    if ('score' in categoryData) {
                                        judgeScoresHTML += `<div style="margin-bottom: 10px;">`;
                                        judgeScoresHTML += `<strong>${formatCriterion(category)}:</strong> <span class="score ${getDetailScoreClass(categoryData.score)}">${categoryData.score}</span>`;
                                        if (categoryData.reasoning) {
                                            judgeScoresHTML += `<p style="color: #666; margin: 5px 0 0 0; font-size: 0.9em;">${categoryData.reasoning}</p>`;
                                        }
                                        judgeScoresHTML += `</div>`;
                                    }
                                    // Nested format
                                    else {
                                        judgeScoresHTML += `<div style="margin-bottom: 10px;"><strong>${formatCriterion(category)}:</strong><ul style="margin: 5px 0;">`;
                                        for (const [criterion, data] of Object.entries(categoryData)) {
                                            if (typeof data === 'object' && 'score' in data) {
                                                judgeScoresHTML += `<li><strong>${formatCriterion(criterion)}:</strong> <span class="score ${getDetailScoreClass(data.score)}">${data.score}</span>`;
                                                if (data.reasoning) {
                                                    judgeScoresHTML += `<br><small style="color: #666;">${data.reasoning}</small>`;
                                                }
                                                judgeScoresHTML += `</li>`;
                                            }
                                        }
                                        judgeScoresHTML += '</ul></div>';
                                    }
                                }
                            }

                            // Show summary if present
                            if (detailedScores.summary?.overall_assessment) {
                                judgeScoresHTML += `<div style="margin-top: 10px; padding: 10px; background: #e8f5e9; border-radius: 4px;">`;
                                judgeScoresHTML += `<strong>Summary:</strong> ${detailedScores.summary.overall_assessment}`;
                                judgeScoresHTML += `</div>`;
                            }
                        }

                        judgeScoresHTML += '</div>';
                    });

                    judgeScoresHTML += '</div>';
                }

                detailContent.innerHTML = `
                    <h2>Submission Details</h2>
                    <div class="metadata">
                        <div class="metadata-item">
                            <div class="metadata-label">Task ID</div>
                            <div class="metadata-value">${sub.task_id}</div>
                        </div>
                        <div class="metadata-item">
                            <div class="metadata-label">Agent ID</div>
                            <div class="metadata-value"><code>${sub.agent_id}</code></div>
                        </div>
                        <div class="metadata-item">
                            <div class="metadata-label">Submission ID</div>
                            <div class="metadata-value"><code>${sub.submission_id}</code></div>
                        </div>
                        <div class="metadata-item">
                            <div class="metadata-label">Grading Schema</div>
                            <div class="metadata-value">${sub.grading_schema || 'N/A'}</div>
                        </div>
                        <div class="metadata-item">
                            <div class="metadata-label">Created</div>
                            <div class="metadata-value">${sub.created_at}</div>
                        </div>
                        <div class="metadata-item">
                            <div class="metadata-label">Judges</div>
                            <div class="metadata-value">${numJudges}</div>
                        </div>
                    </div>

                    ${scoreStatsHTML}
                    ${judgeScoresHTML}

                    <h3>Pseudocode</h3>
                    <pre>${escapeHtml(sub.pseudocode)}</pre>
                `;

                document.getElementById('list-view').style.display = 'none';
                document.getElementById('detail-view').classList.add('active');
            } catch (error) {
                alert('Error loading submission: ' + error);
            }
        }

        function toggleJudgeDetails() {
            const details = document.getElementById('judge-details');
            const btn = document.getElementById('judge-toggle-btn');
            if (details.style.display === 'none') {
                details.style.display = 'block';
                btn.textContent = btn.textContent.replace('Show', 'Hide');
            } else {
                details.style.display = 'none';
                btn.textContent = btn.textContent.replace('Hide', 'Show');
            }
        }

        function showList() {
            document.getElementById('detail-view').classList.remove('active');
            document.getElementById('list-view').style.display = 'block';
        }

        async function loadModeInfo() {
            try {
                const response = await fetch('/api/mode');
                const data = await response.json();
                currentMode = data.mode;

                const indicator = document.getElementById('mode-indicator');
                if (currentMode === 'modal') {
                    indicator.textContent = 'Modal Mode';
                    indicator.style.color = '#2196F3';
                    indicator.title = 'Connected to: ' + data.server_url;
                } else {
                    indicator.textContent = 'Auto-refresh enabled';
                    indicator.title = 'Database: ' + data.db_path;
                }
            } catch (error) {
                console.error('Error loading mode info:', error);
            }
        }

        // Search filtering
        document.addEventListener('DOMContentLoaded', async () => {
            await loadModeInfo();
            loadSubmissions();

            // Only start auto-refresh in local mode
            if (currentMode === 'local') {
                startAutoRefresh();
            }

            document.getElementById('submission-type').addEventListener('change', onSubmissionTypeChange);
            document.getElementById('search-task').addEventListener('input', filterSubmissions);
            document.getElementById('search-agent').addEventListener('input', filterSubmissions);
        });

        // Stop auto-refresh when page is hidden (saves resources)
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopAutoRefresh();
            } else if (currentMode === 'local') {
                startAutoRefresh();
                checkForUpdates(); // Check immediately when page becomes visible
            }
        });

        function filterSubmissions() {
            const taskFilter = document.getElementById('search-task').value.toLowerCase();
            const agentFilter = document.getElementById('search-agent').value.toLowerCase();

            const filtered = allSubmissions.filter(sub => {
                const matchTask = !taskFilter || sub.task_id.toLowerCase().includes(taskFilter);
                const matchAgent = !agentFilter || sub.agent_id.toLowerCase().includes(agentFilter);
                return matchTask && matchAgent;
            });

            displaySubmissions(filtered);
        }
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode())

    def serve_tables(self):
        """List all tables in the database."""
        if CONFIG["mode"] == "modal":
            self.send_json({"tables": ["re_submissions", "ctf_submissions"], "mode": "modal"})
            return

        conn = sqlite3.connect(CONFIG["db_path"])
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        self.send_json({"tables": tables})

    def serve_submissions(self, query):
        """List all submissions with optional filtering."""
        submission_type = query.get("type", ["re"])[0]  # 're', 're-five-point', 're-granular', or 'ctf'

        # Parse schema filter from submission type
        schema_filter = None
        if submission_type.startswith("re-"):
            schema_filter = submission_type[3:]  # Extract schema name after "re-"
            submission_type = "re"

        if CONFIG["mode"] == "modal":
            self._serve_submissions_modal(query, submission_type, schema_filter)
        else:
            self._serve_submissions_local(query, submission_type, schema_filter)

    def _serve_submissions_modal(self, query, submission_type, schema_filter=None):
        """Fetch submissions from Modal server."""
        try:
            if submission_type == "ctf":
                # Query CTF submissions from Modal
                records = modal_api_request("/query-ctf-submissions", {})

                # Group by agent_id and keep only the last submission per agent
                agent_submissions = {}
                for record in records:
                    agent_id = record.get("agent_id")
                    created_at = record.get("created_at")

                    # Handle datetime serialization
                    if isinstance(created_at, dict):
                        created_at = created_at.get("$date", str(created_at))
                    elif hasattr(created_at, "isoformat"):
                        created_at = created_at.isoformat()

                    # Keep track of latest submission per agent
                    if agent_id not in agent_submissions or created_at > agent_submissions[agent_id]["created_at"]:
                        # Get metrics from transcript if available
                        metrics = TRANSCRIPT_METRICS.get(agent_id, {})

                        agent_submissions[agent_id] = {
                            "agent_id": agent_id,
                            "task_id": record.get("task_id"),
                            "submission_id": record.get("submission_id"),
                            "submitted_flag": record.get("submitted_flag", "N/A"),
                            "correct": record.get("correct"),
                            "created_at": created_at,
                            # Metrics from transcript
                            "total_tokens": metrics.get("total_tokens"),
                            "duration_seconds": metrics.get("duration_seconds"),
                        }

                submissions = list(agent_submissions.values())
            else:
                # Query RE submissions from Modal
                records = modal_api_request("/query-re-submissions", {})

                submissions = []
                for record in records:
                    # Parse evaluations JSON if present
                    evaluations = record.get("evaluations")
                    eval_list = json.loads(evaluations) if evaluations else []

                    # Use the first evaluation if available
                    eval_data = eval_list[0] if eval_list else {}

                    # Handle datetime serialization
                    created_at = record.get("created_at")
                    if isinstance(created_at, dict):
                        created_at = created_at.get("$date", str(created_at))
                    elif hasattr(created_at, "isoformat"):
                        created_at = created_at.isoformat()

                    grading_schema = eval_data.get("grading_schema") if eval_data else None

                    # Skip if schema filter is set and doesn't match
                    if schema_filter and grading_schema != schema_filter:
                        continue

                    submissions.append({
                        "agent_id": record.get("agent_id"),
                        "task_id": record.get("task_id"),
                        "submission_id": record.get("submission_id"),
                        "grading_schema": grading_schema,
                        "category_scores": json.dumps(eval_data.get("category_scores")) if eval_data.get("category_scores") else None,
                        "created_at": created_at,
                        "evaluated_at": eval_data.get("evaluated_at") if eval_data else None,
                        "num_evaluations": len(eval_list),
                    })

            # Sort by created_at descending
            submissions.sort(key=lambda x: x.get("created_at") or "", reverse=True)
            self.send_json(submissions)

        except Exception as e:
            self.send_error(500, f"Modal API error: {e}")

    def _serve_submissions_local(self, query, submission_type, schema_filter=None):
        """Fetch submissions from local SQLite database."""
        conn = sqlite3.connect(CONFIG["db_path"])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if submission_type == "ctf":
            cursor.execute("""
                SELECT
                    agent_id,
                    task_id,
                    submission_id,
                    submitted_flag,
                    correct,
                    created_at
                FROM ctf_submissions
                ORDER BY created_at DESC
            """)

            # Group by agent_id and keep only the last submission per agent
            agent_submissions = {}
            for row in cursor.fetchall():
                agent_id = row["agent_id"]
                created_at = row["created_at"]

                # Keep track of latest submission per agent
                if agent_id not in agent_submissions or created_at > agent_submissions[agent_id]["created_at"]:
                    # Get metrics from transcript if available
                    metrics = TRANSCRIPT_METRICS.get(agent_id, {})

                    agent_submissions[agent_id] = {
                        "agent_id": agent_id,
                        "task_id": row["task_id"],
                        "submission_id": row["submission_id"],
                        "submitted_flag": row["submitted_flag"] or "N/A",
                        "correct": row["correct"],
                        "created_at": created_at,
                        # Metrics from transcript
                        "total_tokens": metrics.get("total_tokens"),
                        "duration_seconds": metrics.get("duration_seconds"),
                    }

            submissions = list(agent_submissions.values())
        else:
            cursor.execute("""
                SELECT
                    agent_id,
                    task_id,
                    submission_id,
                    evaluations,
                    created_at
                FROM re_submissions
                ORDER BY created_at DESC
            """)

            submissions = []
            for row in cursor.fetchall():
                # Parse evaluations JSON if present (it's a list of judge evaluations)
                evaluations = row["evaluations"]
                eval_list = json.loads(evaluations) if evaluations else []

                # Use the first evaluation if available
                eval_data = eval_list[0] if eval_list else {}

                grading_schema = eval_data.get("grading_schema") if eval_data else None

                # Skip if schema filter is set and doesn't match
                if schema_filter and grading_schema != schema_filter:
                    continue

                submissions.append({
                    "agent_id": row["agent_id"],
                    "task_id": row["task_id"],
                    "submission_id": row["submission_id"],
                    "grading_schema": grading_schema,
                    "category_scores": json.dumps(eval_data.get("category_scores")) if eval_data.get("category_scores") else None,
                    "created_at": row["created_at"],
                    "evaluated_at": eval_data.get("evaluated_at") if eval_data else None,
                    "num_evaluations": len(eval_list),
                })

        conn.close()
        self.send_json(submissions)

    def serve_submission_detail(self, submission_id):
        """Get full details of a specific submission."""
        if CONFIG["mode"] == "modal":
            self._serve_submission_detail_modal(submission_id)
        else:
            self._serve_submission_detail_local(submission_id)

    def _serve_submission_detail_modal(self, submission_id):
        """Fetch submission detail from Modal server."""
        try:
            # Modal API doesn't have a direct submission_id query, so we get all and filter
            # This is inefficient but works for now
            re_records = modal_api_request("/query-re-submissions", {})

            # Find the submission by ID
            record = None
            for r in re_records:
                if r.get("submission_id") == submission_id:
                    record = r
                    break

            if not record:
                self.send_error(404, "Submission not found")
                return

            # Parse evaluations JSON if present
            evaluations = record.get("evaluations")
            eval_list = json.loads(evaluations) if evaluations else []

            # Use the first evaluation if available
            eval_data = eval_list[0] if eval_list else {}

            # Handle datetime serialization
            created_at = record.get("created_at")
            if isinstance(created_at, dict):
                created_at = created_at.get("$date", str(created_at))
            elif hasattr(created_at, "isoformat"):
                created_at = created_at.isoformat()

            submission = {
                "agent_id": record.get("agent_id"),
                "task_id": record.get("task_id"),
                "submission_id": record.get("submission_id"),
                "pseudocode": record.get("pseudocode", "(Pseudocode not available via Modal API)"),
                "grading_schema": eval_data.get("grading_schema") if eval_data else None,
                "category_scores": json.dumps(eval_data.get("category_scores")) if eval_data.get("category_scores") else None,
                "detailed_scores": eval_data.get("detailed_scores") if eval_data else None,
                "created_at": created_at,
                "evaluated_at": eval_data.get("evaluated_at") if eval_data else None,
                "all_evaluations": eval_list,
            }

            self.send_json(submission)

        except Exception as e:
            self.send_error(500, f"Modal API error: {e}")

    def _serve_submission_detail_local(self, submission_id):
        """Fetch submission detail from local SQLite database."""
        conn = sqlite3.connect(CONFIG["db_path"])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM re_submissions
            WHERE submission_id = ?
        """, (submission_id,))

        row = cursor.fetchone()
        conn.close()

        if not row:
            self.send_error(404, "Submission not found")
            return

        # Parse evaluations JSON if present (it's a list of judge evaluations)
        evaluations = row["evaluations"]
        eval_list = json.loads(evaluations) if evaluations else []

        # Use the first evaluation if available
        eval_data = eval_list[0] if eval_list else {}

        submission = {
            "agent_id": row["agent_id"],
            "task_id": row["task_id"],
            "submission_id": row["submission_id"],
            "pseudocode": row["pseudocode"],
            "grading_schema": eval_data.get("grading_schema") if eval_data else None,
            "category_scores": json.dumps(eval_data.get("category_scores")) if eval_data.get("category_scores") else None,
            "detailed_scores": eval_data.get("detailed_scores") if eval_data else None,
            "created_at": row["created_at"],
            "evaluated_at": eval_data.get("evaluated_at") if eval_data else None,
            "all_evaluations": eval_list,
        }

        self.send_json(submission)

    def serve_db_mtime(self):
        """Return the current database modification time."""
        mtime = get_db_mtime()
        self.send_json({"mtime": mtime})

    def serve_mode_info(self):
        """Return the current mode information."""
        self.send_json({
            "mode": CONFIG["mode"],
            "server_url": CONFIG["server_url"] if CONFIG["mode"] == "modal" else None,
            "db_path": str(CONFIG["db_path"]) if CONFIG["mode"] == "local" else None,
        })

    def send_json(self, data):
        """Send JSON response."""
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


MODAL_SERVER_URL = "https://independentsafetyresearch--cybergym-server-fastapi-app.modal.run"
DEFAULT_API_KEY = "cybergym-030a0cd7-5908-4862-8ab9-91f2bfc7b56d"


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Web-based database viewer for CyberGym submissions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Local mode (default) - uses server_poc/poc.db
    python web_db_viewer.py

    # Local mode with custom database path
    python web_db_viewer.py --db-path /path/to/poc.db

    # Modal mode - uses remote Modal server
    python web_db_viewer.py --modal

    # Modal mode with transcript metrics (tokens, duration)
    python web_db_viewer.py --modal --transcript-dir /path/to/transcripts/modal-test-6
        """,
    )

    parser.add_argument(
        "--modal",
        action="store_true",
        help="Use Modal server instead of local database",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("server_poc/poc.db"),
        help="Path to local SQLite database (default: server_poc/poc.db)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to run the web server on (default: 8765)",
    )
    parser.add_argument(
        "--transcript-dir",
        type=Path,
        default=None,
        help="Path to transcript directory containing summary.json (for token/timing metrics)",
    )

    return parser.parse_args()


def main():
    global TRANSCRIPT_METRICS
    args = parse_args()

    # Configure global settings based on runtime mode
    if args.modal:
        CONFIG["mode"] = "modal"
        CONFIG["server_url"] = MODAL_SERVER_URL
        CONFIG["api_key"] = os.environ.get("CYBERGYM_API_KEY", DEFAULT_API_KEY)
    else:
        CONFIG["mode"] = "local"
        CONFIG["db_path"] = args.db_path

        if not CONFIG["db_path"].exists():
            print(f"Error: Database not found at {CONFIG['db_path']}")
            return 1

    # Load transcript metrics if provided
    if args.transcript_dir:
        CONFIG["transcript_dir"] = args.transcript_dir
        TRANSCRIPT_METRICS = load_transcript_metrics(args.transcript_dir)

    server = HTTPServer(("localhost", args.port), DBViewerHandler)

    print("=" * 70)
    print("CyberGym Database Viewer")
    print("=" * 70)

    if CONFIG["mode"] == "modal":
        print(f"\nMode: Modal (remote server)")
        print(f"Server URL: {CONFIG['server_url']}")
        print(f"Note: Auto-refresh is disabled in Modal mode")
    else:
        print(f"\nMode: Local SQLite")
        print(f"Database: {CONFIG['db_path']}")

    print(f"\nServer running at: http://localhost:{args.port}")
    print("\nOpen this URL in your browser to view the database.")
    print("Press Ctrl+C to stop the server.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServer stopped.")
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
