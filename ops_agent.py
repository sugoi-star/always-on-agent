"""
Always-On Ops Agent — Dashboard Edition
Triages open incidents, writes a polished HTML dashboard to GitHub Pages,
and maintains a run history log.
"""

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
REPO = Path(__file__).parent
RUN_LOG = REPO / "agent_runs.json"

SYSTEM_PROMPT = """You are an expert on-call ops agent. Triage this incident concisely.

Output using EXACTLY this format (no deviations):

SEVERITY: P0|P1|P2|P3
HYPOTHESIS: One sentence root cause.
EVIDENCE:
- evidence point 1
- evidence point 2
- evidence point 3
ACTION: One concrete recommended action — name the service, file, command, or person.
CONFIDENCE: High|Medium|Low"""

SEV_COLOR  = {"P0": "#dc2626", "P1": "#ea580c", "P2": "#d97706", "P3": "#16a34a"}
SEV_BG     = {"P0": "#fef2f2", "P1": "#fff7ed", "P2": "#fffbeb", "P3": "#f0fdf4"}
SEV_BORDER = {"P0": "#fca5a5", "P1": "#fdba74", "P2": "#fcd34d", "P3": "#86efac"}
SEV_BADGE  = {"P0": "#dc2626", "P1": "#ea580c", "P2": "#d97706", "P3": "#16a34a"}


def load_json(path):
    return json.loads(Path(path).read_text())

def load_issues():
    return [load_json(f) for f in sorted((REPO / "issues").glob("*.json"))]

def load_deploys():
    return load_json(REPO / "deploys" / "recent.json")

def load_runbooks():
    return {f.stem: f.read_text() for f in (REPO / "runbooks").glob("*.md")}

def load_run_log():
    if RUN_LOG.exists():
        return json.loads(RUN_LOG.read_text())
    return {"runs": []}

def save_run_log(log):
    RUN_LOG.write_text(json.dumps(log, indent=2))


def triage_issue(issue, deploys, runbooks):
    runbook_text = "\n\n---\n\n".join(f"# {k}\n{v}" for k, v in runbooks.items())
    prompt = f"""Incident:\n{json.dumps(issue, indent=2)}

Recent deploys:\n{json.dumps(deploys, indent=2)}

Runbooks:\n{runbook_text}

Triage this incident now."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_triage(response.content[0].text)


def parse_triage(text):
    result = {"severity": "P3", "hypothesis": "", "evidence": [], "action": "", "confidence": "Medium"}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SEVERITY:"):
            for p in ["P0", "P1", "P2", "P3"]:
                if p in line:
                    result["severity"] = p
        elif line.startswith("HYPOTHESIS:"):
            result["hypothesis"] = line.replace("HYPOTHESIS:", "").strip()
        elif line.startswith("EVIDENCE:"):
            current = "evidence"
        elif line.startswith("ACTION:"):
            current = None
            result["action"] = line.replace("ACTION:", "").strip()
        elif line.startswith("CONFIDENCE:"):
            current = None
            for c in ["High", "Medium", "Low"]:
                if c.lower() in line.lower():
                    result["confidence"] = c
        elif current == "evidence" and line.startswith("- "):
            result["evidence"].append(line[2:])
    return result


def time_ago(iso_str):
    if not iso_str:
        return "unknown"
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def generate_dashboard(open_results, resolved_issues, deploys, run_log, run_time):
    now_utc = run_time

    # Stats
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for r in open_results:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    critical = counts["P0"] + counts["P1"]
    resolved_today = len([i for i in resolved_issues if i.get("resolved_at", "").startswith("2026-05-27")])

    # Sort: P0 first
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    sorted_results = sorted(open_results, key=lambda r: order.get(r["severity"], 9))

    # Alert banner
    critical_ids = [r["issue_id"] for r in sorted_results if r["severity"] in ("P0", "P1")]
    alert_html = ""
    if critical_ids:
        ids_str = " · ".join(critical_ids)
        alert_html = f"""
        <div class="alert-banner">
          <span class="pulse-dot"></span>
          <strong>{len(critical_ids)} critical issue(s) require immediate action</strong>
          <span class="alert-ids">{ids_str}</span>
        </div>"""

    # Incident cards
    cards_html = ""
    for r in sorted_results:
        sev = r["severity"]
        evidence_items = "".join(f"<li>{e}</li>" for e in r["evidence"])
        conf_icon = {"High": "✅", "Medium": "⚠️", "Low": "❓"}.get(r["confidence"], "")
        status_badge = f'<span class="sev-badge" style="background:{SEV_BADGE[sev]}">{sev}</span>'
        cards_html += f"""
        <div class="incident-card" style="border-color:{SEV_BORDER[sev]};background:{SEV_BG[sev]}">
          <div class="card-header">
            {status_badge}
            <span class="issue-id">{r["issue_id"]}</span>
            <span class="issue-title">{r["title"]}</span>
            <span class="conf-badge">{conf_icon} {r["confidence"]}</span>
          </div>
          <div class="card-body">
            <div class="card-section">
              <div class="label">Root Cause</div>
              <div class="value">{r["hypothesis"]}</div>
            </div>
            <div class="card-section">
              <div class="label">Evidence</div>
              <ul class="evidence-list">{evidence_items}</ul>
            </div>
            <div class="card-action">
              <span class="action-label">▶ Recommended Action</span>
              <span class="action-text">{r["action"]}</span>
            </div>
          </div>
        </div>"""

    # Deploy timeline
    deploy_items = ""
    for d in deploys.get("deploys", [])[:6]:
        svc = d.get("service", "")
        ver = d.get("version", "")
        ago = time_ago(d.get("deployed_at", ""))
        summary = d.get("summary", "")
        rollback = "✓ rollback available" if d.get("rollback_available") else "✗ no rollback"
        rb_color = "#16a34a" if d.get("rollback_available") else "#dc2626"
        deploy_items += f"""
        <div class="deploy-item">
          <div class="deploy-header">
            <span class="deploy-svc">{svc}</span>
            <span class="deploy-ver">{ver}</span>
            <span class="deploy-ago">{ago}</span>
          </div>
          <div class="deploy-summary">{summary}</div>
          <div class="deploy-rollback" style="color:{rb_color}">{rollback}</div>
        </div>"""

    # Resolved incidents
    resolved_html = ""
    for i in resolved_issues[-4:][::-1]:
        resolved_html += f"""
        <div class="resolved-item">
          <span class="resolved-id">{i["id"]}</span>
          <span class="resolved-title">{i.get("title","")[:50]}…</span>
          <span class="resolved-age">{time_ago(i.get("resolved_at",""))}</span>
        </div>"""

    # Run history
    runs_html = ""
    for run in run_log.get("runs", [])[-6:][::-1]:
        t = run.get("time", "")[:16].replace("T", " ")
        found = run.get("open_count", 0)
        critical_count = run.get("critical_count", 0)
        dot_color = "#dc2626" if critical_count > 0 else "#16a34a"
        runs_html += f"""
        <div class="run-item">
          <span class="run-dot" style="background:{dot_color}"></span>
          <span class="run-time">{t} UTC</span>
          <span class="run-detail">{found} open · {critical_count} critical</span>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Ops Agent Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f172a; color: #e2e8f0; min-height: 100vh; }}

    /* Header */
    .header {{ background: #1e293b; border-bottom: 1px solid #334155;
               padding: 16px 28px; display: flex; align-items: center;
               justify-content: space-between; }}
    .header-left {{ display: flex; align-items: center; gap: 12px; }}
    .header-title {{ font-size: 18px; font-weight: 700; color: #f1f5f9; }}
    .live-badge {{ display: flex; align-items: center; gap: 6px;
                   background: #064e3b; border: 1px solid #065f46;
                   color: #34d399; padding: 3px 10px; border-radius: 20px;
                   font-size: 12px; font-weight: 600; }}
    .live-dot {{ width: 7px; height: 7px; background: #34d399; border-radius: 50%;
                 animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.4 }} }}
    .header-meta {{ font-size: 12px; color: #64748b; text-align: right; line-height: 1.8; }}
    .header-meta span {{ color: #94a3b8; }}

    /* Stats bar */
    .stats-bar {{ display: grid; grid-template-columns: repeat(5, 1fr);
                  gap: 1px; background: #334155; border-bottom: 1px solid #334155; }}
    .stat {{ background: #1e293b; padding: 16px 20px; text-align: center; }}
    .stat-num {{ font-size: 28px; font-weight: 800; line-height: 1; }}
    .stat-label {{ font-size: 11px; color: #64748b; margin-top: 4px;
                   text-transform: uppercase; letter-spacing: .05em; }}
    .stat.critical .stat-num {{ color: #f87171; }}
    .stat.warning .stat-num  {{ color: #fb923c; }}
    .stat.low .stat-num      {{ color: #4ade80; }}
    .stat.total .stat-num    {{ color: #94a3b8; }}
    .stat.resolved .stat-num {{ color: #60a5fa; }}

    /* Alert banner */
    .alert-banner {{ background: #7f1d1d; border-bottom: 1px solid #991b1b;
                     padding: 12px 28px; display: flex; align-items: center;
                     gap: 12px; font-size: 14px; color: #fecaca; }}
    .pulse-dot {{ width: 10px; height: 10px; background: #f87171; border-radius: 50%;
                  animation: pulse 1s infinite; flex-shrink: 0; }}
    .alert-ids {{ margin-left: auto; font-family: monospace; font-size: 13px;
                  color: #fca5a5; }}

    /* Main layout */
    .main {{ display: grid; grid-template-columns: 1fr 300px;
             gap: 20px; padding: 20px 24px; max-width: 1400px; margin: 0 auto; }}

    /* Incident cards */
    .section-title {{ font-size: 12px; font-weight: 600; color: #64748b;
                      text-transform: uppercase; letter-spacing: .08em;
                      margin-bottom: 12px; }}
    .incident-card {{ border: 1px solid; border-radius: 10px; margin-bottom: 12px;
                      overflow: hidden; }}
    .card-header {{ display: flex; align-items: center; gap: 10px; padding: 12px 16px;
                    background: rgba(0,0,0,.15); flex-wrap: wrap; }}
    .sev-badge {{ color: white; padding: 2px 10px; border-radius: 20px;
                  font-size: 12px; font-weight: 700; flex-shrink: 0; }}
    .issue-id {{ font-family: monospace; font-size: 13px; color: #94a3b8;
                 font-weight: 600; }}
    .issue-title {{ font-size: 14px; color: #e2e8f0; font-weight: 500; flex: 1; }}
    .conf-badge {{ font-size: 12px; color: #64748b; margin-left: auto; }}
    .card-body {{ padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }}
    .card-section {{ display: flex; flex-direction: column; gap: 4px; }}
    .label {{ font-size: 11px; font-weight: 600; color: #64748b;
              text-transform: uppercase; letter-spacing: .06em; }}
    .value {{ font-size: 14px; color: #cbd5e1; line-height: 1.5; }}
    .evidence-list {{ padding-left: 18px; }}
    .evidence-list li {{ font-size: 13px; color: #94a3b8; line-height: 1.6;
                         margin-bottom: 2px; }}
    .card-action {{ background: rgba(0,0,0,.2); border-radius: 6px;
                    padding: 10px 12px; display: flex; gap: 8px; align-items: flex-start; }}
    .action-label {{ font-size: 11px; font-weight: 700; color: #60a5fa;
                     white-space: nowrap; padding-top: 2px; }}
    .action-text {{ font-size: 13px; color: #bfdbfe; line-height: 1.5; }}

    /* Sidebar */
    .sidebar {{ display: flex; flex-direction: column; gap: 20px; }}
    .sidebar-card {{ background: #1e293b; border: 1px solid #334155;
                     border-radius: 10px; padding: 16px; }}

    /* Deploys */
    .deploy-item {{ padding: 10px 0; border-bottom: 1px solid #1e293b; }}
    .deploy-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
    .deploy-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
    .deploy-svc {{ font-size: 13px; font-weight: 600; color: #e2e8f0; }}
    .deploy-ver {{ font-size: 11px; color: #94a3b8; font-family: monospace; }}
    .deploy-ago {{ font-size: 11px; color: #64748b; margin-left: auto; }}
    .deploy-summary {{ font-size: 12px; color: #64748b; margin-bottom: 3px; }}
    .deploy-rollback {{ font-size: 11px; font-weight: 600; }}

    /* Resolved */
    .resolved-item {{ display: flex; align-items: center; gap: 8px; padding: 8px 0;
                      border-bottom: 1px solid #1e293b; }}
    .resolved-item:last-child {{ border-bottom: none; }}
    .resolved-id {{ font-family: monospace; font-size: 12px; color: #60a5fa; flex-shrink: 0; }}
    .resolved-title {{ font-size: 12px; color: #64748b; flex: 1; }}
    .resolved-age {{ font-size: 11px; color: #475569; flex-shrink: 0; }}

    /* Run history */
    .run-item {{ display: flex; align-items: center; gap: 8px; padding: 7px 0;
                 border-bottom: 1px solid #1e293b; }}
    .run-item:last-child {{ border-bottom: none; }}
    .run-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
    .run-time {{ font-size: 12px; color: #94a3b8; font-family: monospace; }}
    .run-detail {{ font-size: 11px; color: #64748b; margin-left: auto; }}

    .footer {{ text-align: center; padding: 24px; color: #334155; font-size: 12px; }}
    .footer span {{ color: #475569; }}
  </style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-title">🔍 Always-On Ops Agent</div>
    <div class="live-badge"><span class="live-dot"></span> LIVE</div>
  </div>
  <div class="header-meta">
    Last run: <span>{now_utc} UTC</span><br>
    Auto-refreshes every 60s · Powered by Claude
  </div>
</div>

<div class="stats-bar">
  <div class="stat critical">
    <div class="stat-num">{critical}</div>
    <div class="stat-label">Critical (P0+P1)</div>
  </div>
  <div class="stat warning">
    <div class="stat-num">{counts.get("P2",0)}</div>
    <div class="stat-label">Warning (P2)</div>
  </div>
  <div class="stat low">
    <div class="stat-num">{counts.get("P3",0)}</div>
    <div class="stat-label">Low (P3)</div>
  </div>
  <div class="stat total">
    <div class="stat-num">{len(open_results)}</div>
    <div class="stat-label">Open</div>
  </div>
  <div class="stat resolved">
    <div class="stat-num">{resolved_today}</div>
    <div class="stat-label">Resolved Today</div>
  </div>
</div>

{alert_html}

<div class="main">
  <div class="incidents-col">
    <div class="section-title">Open Incidents — sorted by severity</div>
    {cards_html}
  </div>

  <div class="sidebar">
    <div class="sidebar-card">
      <div class="section-title">Recent Deploys</div>
      {deploy_items}
    </div>

    <div class="sidebar-card">
      <div class="section-title">Resolved Recently</div>
      {resolved_html if resolved_html else '<div style="color:#475569;font-size:13px;">No resolved incidents</div>'}
    </div>

    <div class="sidebar-card">
      <div class="section-title">Agent Run History</div>
      {runs_html if runs_html else '<div style="color:#475569;font-size:13px;">No runs yet</div>'}
    </div>
  </div>
</div>

<div class="footer">Always-On Ops Agent · <span>sugoi-star/always-on-agent</span> · Refreshes hourly via Claude Routines</div>

</body>
</html>"""


def commit_dashboard(html, run_log):
    (REPO / "index.html").write_text(html)
    save_run_log(run_log)
    subprocess.run(["git", "add", "index.html", "agent_runs.json",
                    "issues/PROD-4531.json", "issues/PROD-4478.json", "issues/PROD-4461.json"],
                   cwd=REPO, capture_output=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(["git", "commit", "-m", f"ops-agent: triage report {ts}"],
                   cwd=REPO, check=True)
    subprocess.run(["git", "push"], cwd=REPO, check=True)
    print("✅ Dashboard pushed → https://sugoi-star.github.io/always-on-agent/")


def run():
    print("🔍 Always-On Ops Agent starting...\n")
    issues   = load_issues()
    deploys  = load_deploys()
    runbooks = load_runbooks()
    run_log  = load_run_log()

    open_issues     = [i for i in issues if i.get("status") == "open"]
    resolved_issues = [i for i in issues if i.get("status") == "resolved"]
    print(f"Found {len(open_issues)} open, {len(resolved_issues)} resolved.\n{'='*60}\n")

    results = []
    for issue in open_issues:
        print(f"Triaging {issue['id']}...")
        triage = triage_issue(issue, deploys, runbooks)
        triage["issue_id"] = issue["id"]
        triage["title"]    = issue.get("title", "")
        results.append(triage)
        print(f"  → {triage['severity']} ({triage['confidence']} confidence)")

    # Update run log
    critical_count = sum(1 for r in results if r["severity"] in ("P0", "P1"))
    run_log["runs"].append({
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open_count": len(open_issues),
        "critical_count": critical_count,
    })
    run_log["runs"] = run_log["runs"][-20:]  # keep last 20

    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    html = generate_dashboard(results, resolved_issues, deploys, run_log, run_time)
    print("\nPushing dashboard...")
    commit_dashboard(html, run_log)


if __name__ == "__main__":
    run()
