#!/usr/bin/env python3
"""
Web-based SQLite database viewer for CyberGym submissions.
Runs a local web server to browse the database.
Auto-refreshes when database changes are detected.
"""
import sqlite3
import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import json
import html


DB_PATH = Path("server_poc/poc.db")

# Track database modification time for auto-refresh
db_mtime = None


def get_db_mtime():
    """Get the last modification time of the database."""
    if DB_PATH.exists():
        return os.path.getmtime(DB_PATH)
    return None


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
        else:
            self.send_error(404)

    def serve_index(self):
        """Serve the main HTML page."""
        html_content = """
<!DOCTYPE html>
<html>
<head>
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
            <span>🔒 CyberGym Database Viewer</span>
            <span class="auto-refresh-indicator">Auto-refresh enabled</span>
        </h1>

        <div id="list-view">
            <div class="filter">
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

        function getScoreClass(score) {
            if (score === null || score === undefined) return '';
            if (score >= 0.7) return 'high';
            if (score >= 0.4) return 'medium';
            return 'low';
        }

        function formatScore(score) {
            if (score === null || score === undefined) return 'N/A';
            return score.toFixed(2);
        }

        async function loadSubmissions(silent = false) {
            try {
                const response = await fetch('/api/submissions');
                allSubmissions = await response.json();
                displaySubmissions(allSubmissions);

                if (!silent) {
                    // Update the last known mtime after loading
                    const mtimeResponse = await fetch('/api/db-mtime');
                    const mtimeData = await mtimeResponse.json();
                    lastDbMtime = mtimeData.mtime;
                }
            } catch (error) {
                if (!silent) {
                    document.querySelector('.loading').textContent = 'Error loading submissions: ' + error;
                }
            }
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
            tbody.innerHTML = '';

            submissions.forEach(sub => {
                const row = document.createElement('tr');
                row.onclick = () => loadSubmissionDetail(sub.submission_id);

                // Parse category scores
                let scoresHTML = 'N/A';
                if (sub.category_scores) {
                    try {
                        const scores = JSON.parse(sub.category_scores);
                        const scoreEntries = Object.entries(scores).map(([cat, score]) => {
                            const shortCat = cat.split('_').map(w => w[0].toUpperCase()).join('');
                            return `<span class="score ${getScoreClass(score)}">${shortCat}: ${formatScore(score)}</span>`;
                        }).join(' ');
                        scoresHTML = scoreEntries;
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

                // Parse category scores
                let categoryScoresHTML = '';
                if (sub.category_scores) {
                    try {
                        const scores = JSON.parse(sub.category_scores);
                        categoryScoresHTML = '<h3>Category Scores</h3><div class="metadata">';
                        for (const [category, score] of Object.entries(scores)) {
                            categoryScoresHTML += `
                                <div class="metadata-item">
                                    <div class="metadata-label">${formatCriterion(category)}</div>
                                    <div class="metadata-value"><span class="score ${getScoreClass(score)}">${formatScore(score)}</span></div>
                                </div>
                            `;
                        }
                        categoryScoresHTML += '</div>';
                    } catch (e) {
                        categoryScoresHTML = '<p>Error parsing category scores</p>';
                    }
                }

                // Parse detailed scores (per-criterion breakdown)
                let detailedScoresHTML = '';
                if (sub.detailed_scores) {
                    try {
                        const scores = JSON.parse(sub.detailed_scores);

                        detailedScoresHTML = '<h3>Detailed Scoring Breakdown</h3>';

                        // Dynamically render all categories
                        for (const [category, criteria] of Object.entries(scores)) {
                            if (category === 'summary') continue; // Skip summary section

                            if (typeof criteria === 'object' && criteria !== null) {
                                detailedScoresHTML += `<h4>${formatCriterion(category)}</h4><ul>`;
                                for (const [criterion, data] of Object.entries(criteria)) {
                                    if (typeof data === 'object' && 'score' in data) {
                                        // Map score to color class (-2 to 2 scale)
                                        let scoreClass = 'medium';
                                        if (data.score >= 1) scoreClass = 'high';
                                        else if (data.score <= -1) scoreClass = 'low';

                                        detailedScoresHTML += `<li><strong>${formatCriterion(criterion)}:</strong> <span class="score ${scoreClass}">${data.score}</span>`;
                                        if (data.reasoning) {
                                            detailedScoresHTML += `<br><small style="color: #666;">${data.reasoning}</small>`;
                                        }
                                        detailedScoresHTML += `</li>`;
                                    }
                                }
                                detailedScoresHTML += '</ul>';
                            }
                        }

                        // Show summary if present
                        if (scores.summary) {
                            detailedScoresHTML += '<h4>Summary</h4><ul>';
                            if (scores.summary.total_score !== undefined) {
                                detailedScoresHTML += `<li><strong>Total Score:</strong> ${scores.summary.total_score} / ${scores.summary.max_possible || 20}</li>`;
                            }
                            if (scores.summary.overall_assessment) {
                                detailedScoresHTML += `<li><strong>Assessment:</strong> ${scores.summary.overall_assessment}</li>`;
                            }
                            detailedScoresHTML += '</ul>';
                        }
                    } catch (e) {
                        detailedScoresHTML = '<p>Error parsing detailed scores</p>';
                    }
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
                            <div class="metadata-label">Evaluated</div>
                            <div class="metadata-value">${sub.evaluated_at || 'Not evaluated'}</div>
                        </div>
                    </div>

                    ${categoryScoresHTML}
                    ${detailedScoresHTML}

                    <h3>Pseudocode</h3>
                    <pre>${sub.pseudocode}</pre>
                `;

                document.getElementById('list-view').style.display = 'none';
                document.getElementById('detail-view').classList.add('active');
            } catch (error) {
                alert('Error loading submission: ' + error);
            }
        }

        function showList() {
            document.getElementById('detail-view').classList.remove('active');
            document.getElementById('list-view').style.display = 'block';
        }

        // Search filtering
        document.addEventListener('DOMContentLoaded', () => {
            loadSubmissions();
            startAutoRefresh();

            document.getElementById('search-task').addEventListener('input', filterSubmissions);
            document.getElementById('search-agent').addEventListener('input', filterSubmissions);
        });

        // Stop auto-refresh when page is hidden (saves resources)
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stopAutoRefresh();
            } else {
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
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html_content.encode())

    def serve_tables(self):
        """List all tables in the database."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()

        self.send_json({"tables": tables})

    def serve_submissions(self, query):
        """List all submissions with optional filtering."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                agent_id,
                task_id,
                submission_id,
                grading_schema,
                category_scores,
                created_at,
                evaluated_at
            FROM re_submissions
            ORDER BY created_at DESC
        """)

        submissions = []
        for row in cursor.fetchall():
            submissions.append({
                "agent_id": row["agent_id"],
                "task_id": row["task_id"],
                "submission_id": row["submission_id"],
                "grading_schema": row["grading_schema"],
                "category_scores": row["category_scores"],
                "created_at": row["created_at"],
                "evaluated_at": row["evaluated_at"],
            })

        conn.close()
        self.send_json(submissions)

    def serve_submission_detail(self, submission_id):
        """Get full details of a specific submission."""
        conn = sqlite3.connect(DB_PATH)
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

        submission = {
            "agent_id": row["agent_id"],
            "task_id": row["task_id"],
            "submission_id": row["submission_id"],
            "pseudocode": row["pseudocode"],
            "grading_schema": row["grading_schema"],
            "category_scores": row["category_scores"],
            "detailed_scores": row["detailed_scores"],
            "created_at": row["created_at"],
            "evaluated_at": row["evaluated_at"],
        }

        self.send_json(submission)

    def serve_db_mtime(self):
        """Return the current database modification time."""
        mtime = get_db_mtime()
        self.send_json({"mtime": mtime})

    def send_json(self, data):
        """Send JSON response."""
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def main():
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        return 1

    port = 8765
    server = HTTPServer(("localhost", port), DBViewerHandler)

    print("=" * 70)
    print("🌐 CyberGym Database Viewer")
    print("=" * 70)
    print(f"\nServer running at: http://localhost:{port}")
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
