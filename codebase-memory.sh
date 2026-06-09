#!/usr/bin/env bash
# codebase-memory.sh — Interactive helper for codebase-memory-mcp

set -euo pipefail

CMD="codebase-memory-mcp cli"
DIVIDER="─────────────────────────────────────────"

# ── helpers ────────────────────────────────────────────────────────────────────

run() {
  echo ""
  echo "Running: $*"
  echo "$DIVIDER"
  eval "$@"
  echo "$DIVIDER"
}

pick_project() {
  echo ""
  echo "Fetching indexed projects..."
  $CMD list_projects
  echo ""
  read -rp "Enter project key: " PROJECT
}

# ── main menu ──────────────────────────────────────────────────────────────────

echo ""
echo "  codebase-memory-mcp helper"
echo "$DIVIDER"
echo "  1) Index a repository"
echo "  2) List all indexed projects"
echo "  3) Architecture overview"
echo "  4) Search for classes / functions"
echo "  5) Trace call path"
echo "  6) Detect changes (git diff → affected symbols)"
echo "  7) Manage ADR (architectural notes)"
echo "  8) Read a file"
echo "  9) List a directory"
echo " 10) Search code (grep-like)"
echo " 11) Custom Cypher query"
echo " 12) Delete a project"
echo ""
read -rp "Choose an option [1-12]: " CHOICE

case "$CHOICE" in

# ── 1. Index ──────────────────────────────────────────────────────────────────
1)
  DEFAULT_PATH="$(pwd)"
  echo ""
  read -rp "Repository path [${DEFAULT_PATH}]: " REPO_PATH
  REPO_PATH="${REPO_PATH:-$DEFAULT_PATH}"
  REPO_PATH="$(realpath "$REPO_PATH")"
  run $CMD index_repository "{\"repo_path\": \"$REPO_PATH\"}"
  echo "Done! Use the project key above in future commands."
  ;;

# ── 2. List projects ──────────────────────────────────────────────────────────
2)
  run $CMD list_projects
  ;;

# ── 3. Architecture overview ──────────────────────────────────────────────────
3)
  pick_project
  run $CMD get_architecture "{\"project\": \"$PROJECT\"}"
  ;;

# ── 4. Search graph ───────────────────────────────────────────────────────────
4)
  pick_project
  echo ""
  echo "Label options: Class, Function, Method, Interface, File (leave blank for all)"
  read -rp "Label [Class]: " LABEL
  LABEL="${LABEL:-Class}"
  read -rp "Name pattern (regex, e.g. .*Service.*): " PATTERN
  run $CMD search_graph "{\"project\": \"$PROJECT\", \"label\": \"$LABEL\", \"name_pattern\": \"$PATTERN\"}"
  ;;

# ── 5. Trace call path ────────────────────────────────────────────────────────
5)
  pick_project
  echo ""
  read -rp "Function/method name: " FUNC
  echo ""
  echo "Direction options: callers | callees | both"
  read -rp "Direction [both]: " DIRECTION
  DIRECTION="${DIRECTION:-both}"
  read -rp "Depth [2]: " DEPTH
  DEPTH="${DEPTH:-2}"
  run $CMD trace_call_path "{\"project\": \"$PROJECT\", \"function_name\": \"$FUNC\", \"direction\": \"$DIRECTION\", \"depth\": $DEPTH}"
  ;;

# ── 6. Detect changes ─────────────────────────────────────────────────────────
6)
  pick_project
  run $CMD detect_changes "{\"project\": \"$PROJECT\"}"
  ;;

# ── 7. Manage ADR ─────────────────────────────────────────────────────────────
7)
  pick_project
  echo ""
  echo "ADR = Architectural Decision Record. A freeform notes field attached to"
  echo "your project — store patterns, major decisions, or context for AI agents."
  echo ""
  echo "Mode options:"
  echo "  retrieve — print the current notes"
  echo "  update   — write/replace the notes"
  read -rp "Mode [retrieve]: " MODE
  MODE="${MODE:-retrieve}"

  if [[ "$MODE" == "update" ]]; then
    echo ""
    echo "Enter your architectural notes below."
    echo "Type a single dot (.) on its own line when done:"
    CONTENT=""
    while IFS= read -r line; do
      [[ "$line" == "." ]] && break
      CONTENT+="$line\n"
    done
    # Escape for JSON
    CONTENT_ESCAPED=$(printf '%s' "$CONTENT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null || \
                       printf '%s' "$CONTENT" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' 'n' | sed 's/n$//')
    run $CMD manage_adr "{\"project\": \"$PROJECT\", \"mode\": \"update\", \"content\": $CONTENT_ESCAPED}"
  else
    run $CMD manage_adr "{\"project\": \"$PROJECT\", \"mode\": \"retrieve\"}"
  fi
  ;;

# ── 8. Read file ──────────────────────────────────────────────────────────────
8)
  pick_project
  echo ""
  read -rp "File path (e.g. src/foo.ts): " FILE_PATH
  run $CMD read_file "{\"project\": \"$PROJECT\", \"file_path\": \"$FILE_PATH\"}"
  ;;

# ── 9. List directory ─────────────────────────────────────────────────────────
9)
  pick_project
  echo ""
  read -rp "Directory path (e.g. src/backend): " DIR_PATH
  run $CMD list_directory "{\"project\": \"$PROJECT\", \"path\": \"$DIR_PATH\"}"
  ;;

# ── 10. Search code ───────────────────────────────────────────────────────────
10)
  pick_project
  echo ""
  read -rp "Search query (e.g. ISessionManager): " QUERY
  run $CMD search_code "{\"project\": \"$PROJECT\", \"query\": \"$QUERY\"}"
  ;;

# ── 11. Custom Cypher ─────────────────────────────────────────────────────────
11)
  pick_project
  echo ""
  echo "Example: MATCH (a)-[:FILE_CHANGES_WITH]->(b) RETURN a.name, b.name LIMIT 20"
  read -rp "Cypher query: " QUERY
  QUERY_ESCAPED=$(printf '%s' "$QUERY" | sed 's/"/\\"/g')
  run $CMD query_graph "{\"project\": \"$PROJECT\", \"query\": \"$QUERY_ESCAPED\"}"
  ;;

# ── 12. Delete project ────────────────────────────────────────────────────────
12)
  pick_project
  echo ""
  read -rp "Are you sure you want to delete '$PROJECT'? [y/N]: " CONFIRM
  if [[ "${CONFIRM,,}" == "y" ]]; then
    run $CMD delete_project "{\"project\": \"$PROJECT\"}"
  else
    echo "Aborted."
  fi
  ;;

*)
  echo "Invalid option."
  exit 1
  ;;

esac
