Dưới đây là quy trình triển khai đầy đủ từ A→Z: tạo cấu trúc thư mục, file cấu hình, script “xoá an toàn” (nén + kèm INFO.txt + ghi log + tuỳ chọn đẩy lên remote), script khôi phục, alias/PATH, và dọn rác định kỳ. Làm lần lượt, copy–paste là chạy được.

1) Tạo cây thư mục chuẩn
bash
Sao chép mã
# 1.1. Khai báo biến gốc
BASE="$HOME/safe-delete"

# 1.2. Tạo cấu trúc
mkdir -p "$BASE"/{scripts,backups,logs,tmp}

# (tuỳ chọn) kiểm tra
tree -L 2 "$BASE" 2>/dev/null || find "$BASE" -maxdepth 2 -type d
Cây chuẩn:

arduino
Sao chép mã
~/safe-delete/
├─ scripts/    # chứa code shell
├─ backups/    # nơi lưu file .tar.gz
├─ logs/       # backup.log, lỗi, …
└─ tmp/        # thư mục tạm
2) Tạo file cấu hình hệ thống
bash
Sao chép mã
cat > "$BASE/config.env" <<'EOF'
# === cấu hình safe-delete ===
BACKUP_DIR="$HOME/safe-delete/backups"
LOG_FILE="$HOME/safe-delete/logs/backup.log"

# Số ngày giữ file backup trước khi dọn
RETENTION_DAYS=30

# Hỏi lý do xoá? (true/false)
ASK_REASON=true

# Đẩy bản backup lên remote? (để trống nếu không dùng)
# Ví dụ: "user@server:/data/backup/safe-delete"
REMOTE_TARGET=""

# Cách đẩy: rsync hoặc scp
REMOTE_CMD="rsync"

# Giới hạn kích thước (MB) để tạo checksum SHA256 cho file đơn (0 = tắt)
CHECKSUM_MB=50
EOF
3) Script xoá an toàn: scripts/safe_rm.sh
Hiển thị thông tin file/du/stat

Hỏi xác nhận

Hỏi “lý do xoá” (nếu bật)

Tạo INFO.txt (metadata)

Nén: đối tượng + INFO.txt ⇒ .tar.gz

Ghi backup.log (dạng JSONL, 1 dòng/1 đối tượng)

(Tuỳ chọn) Đẩy lên remote (rsync/scp)

Xoá bản gốc nếu nén OK

Có --prune để dọn rác cũ, -y để auto-yes, -n dry-run

bash
Sao chép mã
cat > "$BASE/scripts/safe_rm.sh" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_CONFIG="$BASE_DIR/config.env"

# Load config
if [[ -f "${DEFAULT_CONFIG}" ]]; then
  # shellcheck disable=SC1090
  source "${DEFAULT_CONFIG}"
fi

# Defaults if not set
BACKUP_DIR="${BACKUP_DIR:-$BASE_DIR/backups}"
LOG_FILE="${LOG_FILE:-$BASE_DIR/logs/backup.log}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
ASK_REASON="${ASK_REASON:-true}"
REMOTE_TARGET="${REMOTE_TARGET:-}"
REMOTE_CMD="${REMOTE_CMD:-rsync}"
CHECKSUM_MB="${CHECKSUM_MB:-0}"

mkdir -p "$BACKUP_DIR" "$BASE_DIR/logs" "$BASE_DIR/tmp"

YES_ALL=false
DRY_RUN=false
PRUNE_ONLY=false
QUIET=false

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options] <file_or_dir>...

Options:
  -y            Auto-yes (không hỏi xác nhận)
  -n            Dry-run (không xoá, không ghi log, không nén)
  -q            Quiet (ít lời)
  --prune       Chỉ dọn rác theo RETENTION_DAYS, không xử lý đối tượng
  -h, --help    Trợ giúp

Mẹo: cấu hình tại $BASE_DIR/config.env
USAGE
}

log() { $QUIET || echo -e "$*"; }

confirm() {
  local prompt="$1"
  if $YES_ALL; then
    return 0
  fi
  read -r -p "$prompt (y/n): " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

dangerous_path() {
  local p="$1"
  # từ chối "/" hoặc rỗng
  [[ -z "$p" || "$p" == "/" ]]
}

size_human() {
  du -sh --apparent-size "$1" 2>/dev/null | awk '{print $1}'
}

sha256_if_small() {
  local f="$1"
  local lim_mb="$2"
  [[ -f "$f" ]] || return 0
  [[ "$lim_mb" -gt 0 ]] || return 0
  # tính cho file đơn nhỏ hơn ngưỡng
  local sz
  sz=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
  local lim=$((lim_mb * 1024 * 1024))
  if (( sz > 0 && sz <= lim )); then
    sha256sum "$f" | awk '{print $1}'
  fi
}

print_info() {
  local item="$1"
  log "----------------------------------------"
  log "Đối tượng: $item"
  file "$item" || true
  size_human "$item" || true
  stat "$item" || true
  log "----------------------------------------"
}

make_info_txt() {
  local item="$1" info="$2"
  local tmp_dir="$3"
  {
    echo "Tên gốc      : $item"
    echo "Người thao tác: ${USER:-unknown}"
    echo "Thời gian    : $(date -Is)"
    echo "Loại         : $(file -b "$item" 2>/dev/null || echo NA)"
    echo "Dung lượng   : $(size_human "$item" || echo NA)"
    if [[ -f "$item" ]]; then
      local sum
      sum=$(sha256_if_small "$item" "${CHECKSUM_MB:-0}" || true)
      [[ -n "$sum" ]] && echo "SHA256       : $sum"
    fi
    echo "Lý do xoá    : ${info:-(không cung cấp)}"
    echo "Chi tiết stat:"
    stat "$item" 2>/dev/null || true
  } > "$tmp_dir/INFO.txt"
}

archive_item() {
  local item="$1" backup_path="$2" tmp_dir="$3"
  # tar cả item và INFO.txt (2 nguồn khác thư mục)
  tar -czf "$backup_path" \
    -C "$(dirname "$item")" "$(basename "$item")" \
    -C "$tmp_dir" INFO.txt
}

write_log_jsonl() {
  local status="$1" item="$2" backup="$3" reason="$4"
  local sz; sz=$(size_human "$item" || echo "NA")
  local now; now=$(date -Is)
  mkdir -p "$(dirname "$LOG_FILE")"
  printf '{"time":"%s","user":"%s","item":"%s","size":"%s","backup":"%s","status":"%s","reason":%s}\n' \
    "$now" "${USER:-unknown}" "$item" "$sz" "${backup:-}" "$status" \
    "$(printf '%s' "$reason" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')" \
    >> "$LOG_FILE"
}

push_remote() {
  local backup="$1"
  [[ -n "$REMOTE_TARGET" ]] || return 0
  case "$REMOTE_CMD" in
    rsync) rsync -av "$backup" "$REMOTE_TARGET"/ ;;
    scp)   scp -q "$backup" "$REMOTE_TARGET"/ ;;
    *)     echo "REMOTE_CMD không hỗ trợ: $REMOTE_CMD" >&2; return 1 ;;
  esac
}

prune_old() {
  [[ "$RETENTION_DAYS" -gt 0 ]] || return 0
  find "$BACKUP_DIR" -type f -name '*_backup_*.tar.gz' -mtime +$RETENTION_DAYS -print -delete 2>/dev/null || true
}

# === parse args ===
while (( "$#" )); do
  case "$1" in
    -y) YES_ALL=true; shift ;;
    -n) DRY_RUN=true; shift ;;
    -q) QUIET=true; shift ;;
    --prune) PRUNE_ONLY=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*)
      echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *) break ;;
  esac
done

if $PRUNE_ONLY; then
  log "Đang dọn backup cũ (> $RETENTION_DAYS ngày) trong $BACKUP_DIR ..."
  prune_old
  log "Xong."
  exit 0
fi

if [[ "$#" -lt 1 ]]; then
  usage; exit 1
fi

for ITEM in "$@"; do
  if dangerous_path "$ITEM"; then
    echo "Từ chối đường dẫn nguy hiểm: '$ITEM'" >&2
    continue
  fi
  if [[ ! -e "$ITEM" ]]; then
    echo "Cảnh báo: Không tồn tại: $ITEM" >&2
    continue
  fi

  print_info "$ITEM"

  if ! $DRY_RUN && ! confirm "Nén + backup & XOÁ '$ITEM'?"; then
    log "Bỏ qua: $ITEM"; continue
  fi

  REASON=""
  if [[ "${ASK_REASON,,}" == "true" ]] && ! $DRY_RUN; then
    read -r -p "Lý do xoá (Enter để bỏ qua): " REASON || true
  fi

  BASENAME=$(basename "$ITEM")
  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  BACKUP_PATH="$BACKUP_DIR/${BASENAME}_backup_${TIMESTAMP}.tar.gz"

  if $DRY_RUN; then
    log "[DRY-RUN] Sẽ tạo: $BACKUP_PATH, rồi xoá $ITEM"
    continue
  fi

  TMP_DIR=$(mktemp -d "${TMPDIR:-$BASE_DIR/tmp}/sd.XXXXXXXX")
  trap 'rm -rf "$TMP_DIR"' RETURN

  make_info_txt "$ITEM" "$REASON" "$TMP_DIR"
  log "Đang nén '$ITEM' + INFO.txt → $BACKUP_PATH ..."
  if archive_item "$ITEM" "$BACKUP_PATH" "$TMP_DIR"; then
    log "Nén OK: $BACKUP_PATH"
    if [[ -n "$REMOTE_TARGET" ]]; then
      log "Đẩy backup lên remote ($REMOTE_CMD → $REMOTE_TARGET) ..."
      push_remote "$BACKUP_PATH" || log "⚠️  Đẩy remote thất bại (bỏ qua)."
    fi
    log "Xoá bản gốc: $ITEM ..."
    rm -rf -- "$ITEM"
    write_log_jsonl "deleted" "$ITEM" "$BACKUP_PATH" "$REASON"
    log "✔ Hoàn tất: $ITEM"
  else
    write_log_jsonl "archive_failed" "$ITEM" "" "$REASON"
    echo "❌ Nén thất bại. KHÔNG xoá '$ITEM'." >&2
  fi
done

# Dọn backup cũ sau mỗi lần chạy
prune_old
EOF

chmod +x "$BASE/scripts/safe_rm.sh"
4) Script khôi phục: scripts/safe_restore.sh
Giải nén một archive .tar.gz về thư mục đích

In ra INFO.txt trước khi restore (để biết bối cảnh)

Tuỳ chọn --list chỉ liệt kê nội dung

bash
Sao chép mã
cat > "$BASE/scripts/safe_restore.sh" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--list] [--to <dir>] <archive.tar.gz>

  --list       Chỉ liệt kê nội dung archive và in INFO.txt
  --to <dir>   Thư mục đích để giải nén (mặc định: thư mục hiện tại)

Ví dụ:
  safe_restore.sh ~/safe-delete/backups/testdir_backup_20250904_123000.tar.gz
USAGE
}

LIST_ONLY=false
TARGET_DIR="."

while (( "$#" )); do
  case "$1" in
    --list) LIST_ONLY=true; shift ;;
    --to) TARGET_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) ARCHIVE="$1"; shift ;;
  esac
done

[[ -n "${ARCHIVE:-}" ]] || { usage; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Không tìm thấy: $ARCHIVE" >&2; exit 1; }

echo "==> Nội dung archive:"
tar -tzf "$ARCHIVE" || { echo "Không đọc được archive." >&2; exit 1; }

# In INFO.txt nếu có
if tar -tzf "$ARCHIVE" | grep -q '^INFO\.txt$'; then
  echo
  echo "==> INFO.txt:"
  tar -xOzf "$ARCHIVE" INFO.txt || true
fi

$LIST_ONLY && exit 0

mkdir -p "$TARGET_DIR"
echo
echo "==> Giải nén về: $TARGET_DIR"
tar -xzf "$ARCHIVE" -C "$TARGET_DIR"
echo "✔ Hoàn tất."
EOF

chmod +x "$BASE/scripts/safe_restore.sh"
5) Thêm PATH hoặc alias cho tiện dùng
bash
Sao chép mã
# Thêm vào ~/.bashrc (hoặc ~/.zshrc)
echo 'export PATH="$HOME/safe-delete/scripts:$PATH"' >> ~/.bashrc

# (tuỳ chọn) thay thế rm bằng script an toàn
echo 'alias rm="safe_rm.sh"' >> ~/.bashrc

# Áp dụng ngay
source ~/.bashrc
Từ giờ có thể gọi:

bash
Sao chép mã
safe_rm.sh testdir file1.txt
# hoặc
safe_rm.sh -y --prune
safe_restore.sh --list "$HOME/safe-delete/backups/testdir_backup_*.tar.gz"
6) Kiểm thử nhanh
bash
Sao chép mã
# Tạo dữ liệu mẫu
mkdir -p ~/demo/testdir && echo "abc" > ~/demo/testdir/a.txt
dd if=/dev/urandom of=~/demo/data.bin bs=1M count=1 status=none

# Chạy xoá an toàn (có xác nhận)
safe_rm.sh ~/demo/testdir ~/demo/data.bin

# Kiểm tra backup & log
ls -lh ~/safe-delete/backups
tail -n 5 ~/safe-delete/logs/backup.log

# Thử khôi phục
ARCH=$(ls -1t ~/safe-delete/backups/*_backup_*.tar.gz | head -1)
safe_restore.sh --list "$ARCH"
mkdir -p ~/restore && safe_restore.sh --to ~/restore "$ARCH"
7) Dọn rác định kỳ (cron)
bash
Sao chép mã
# Mở crontab
crontab -l 2>/dev/null; echo '0 3 * * * '"$HOME"'/safe-delete/scripts/safe_rm.sh --prune >/dev/null 2>&1' | crontab -
→ Mỗi ngày 03:00 sẽ xoá các .tar.gz cũ hơn RETENTION_DAYS.

8) (Tuỳ chọn) Đẩy backup lên remote
Mở ~/safe-delete/config.env và khai:

bash
Sao chép mã
REMOTE_TARGET="user@server:/data/backup/safe-delete"
REMOTE_CMD="rsync"  # hoặc "scp"
Script sẽ tự rsync/scp cập nhật sau mỗi lần nén.

9) Nguyên tắc an toàn bổ sung (đã cài sẵn trong script)
Từ chối xoá đường dẫn nguy hiểm "/" hoặc rỗng.

Luôn hiển thị file/du/stat trước khi thao tác.

Ghi log dạng JSONL để dễ grep/parse:

bash
Sao chép mã
jq . ~/safe-delete/logs/backup.log | less
--prune tách biệt, tránh “dọn rác” khi không cần.

Tóm tắt thao tác hằng ngày
Xoá an toàn:
safe_rm.sh <file_or_dir> ...

Xem & khôi phục:
safe_restore.sh --list <archive> → xem INFO.txt
safe_restore.sh --to <dir> <archive> → giải nén

Dọn rác cũ:
safe_rm.sh --prune

Nếu muốn, tôi có thể gộp hai script vào một file có subcommand (delete|restore|prune). Nhưng với nhu cầu hiện tại, tách file giúp rõ ràng, dễ bảo trì.