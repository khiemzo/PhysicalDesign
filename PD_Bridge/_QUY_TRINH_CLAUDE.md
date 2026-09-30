# Quy trình cho Claude — PD_Bridge

Tài liệu cho Claude (và người bảo trì). Người hỏi chỉ cần đọc `HUONG_DAN.md`.

## 1. Luồng tự động (watcher, không cần chat)

```
cau_hoi/[mode] abc.md (hoặc thư mục kèm dữ liệu)  --(ổn định 2s)-->  đổi tên Q007_abc.md + Q007_traloi.md "⏳ Đã nhận"
     --> claude -p (model theo mode, cwd = PD_Bridge, --add-dir <physical design> <runs\Q007..>)
     --> Claude ghi runs\Q007-v1-...\answer.md
     --> bridge thêm tiêu đề + dòng danh_gia + marker -> cau_hoi/Q007_traloi.md
```

- Phát hiện file mới / sửa file: script Python quét thư mục mỗi giây (**0 token**); tên file [nhanh]/[chuan]/[sau](-model) chọn mode/model; thư mục hoặc file cùng tên gốc = dữ liệu đính kèm (chuyển vào du_lieu_gui/Qnnn_*, tóm tắt sẵn trong prompt).
- File trả lời có marker cuối `<!-- pd_bridge q=Q007 v=1 hash=… status=done … -->`.
  `hash` là hash nội dung câu hỏi lúc trả lời → câu hỏi bị sửa (hash khác) thì trả lời lại (v2),
  bản cũ chuyển thành `Q007_traloi_cu.md`, prompt mới kèm diff câu hỏi + đường dẫn bản cũ + đánh giá.
- Trạng thái trong marker: `queued` (đã nhận), `running`, `waiting` (hết hạn mức/chưa đăng nhập/lỗi tạm),
  `done`, `error` (thử 3 lần vẫn lỗi; sửa câu hỏi để thử lại).
- Hết hạn mức: Opus → thử lại ngay bằng Sonnet; cả gói hết → đọc giờ reset trong thông báo lỗi, tạm dừng
  đến giờ đó rồi tự làm tiếp. Kiểm tra đăng nhập bằng `claude auth status` (0 token) trước mỗi lượt.
- `ANTHROPIC_API_KEY` bị gỡ khỏi môi trường của `claude` → luôn dùng gói Claude, không phát sinh tiền API.
- Giữ 5 lượt mới nhất trong `cau_hoi/`; lượt cũ → `cau_hoi/_cho_xoa/<thời điểm>/`, xoá sau 60 phút.
  Nội dung chính (câu hỏi, tóm tắt, đánh giá) đã ghi vào `tong_hop/nhat_ky.jsonl` trước khi chuyển.
- Chỉ mục `pd_index.py build` chạy nền mỗi 24 giờ (tăng dần theo size/mtime).
- Tổng hợp tuần: sau thứ Sáu 16:55 (hoặc lần bật máy kế tiếp) — `bridge.py` tính thống kê, gọi Claude
  (Sonnet) viết `tong_hop/<năm>-W<tuần>.md` + cập nhật `kien_thuc/kinh_nghiem.md`. Tuần không có câu hỏi: 0 token.

## 2. Khi người dùng nhắn "làm" trong chat đã liên kết với máy

Trả lời ngay trong phiên chat (không chờ watcher, không tốn thêm một lượt `claude -p`):

1. Chạy: `python "<PD_Bridge>\tools\bridge.py" claim --now`
   → nhận mọi câu hỏi đang chờ, in prompt đầy đủ cho từng câu (kèm gợi ý chỉ mục, kiến thức liên quan, file đích).
   Watcher sẽ bỏ qua các câu này trong 3 giờ.
2. Với mỗi câu: làm đúng theo prompt và `CLAUDE.md` (tìm trên máy + web), ghi câu trả lời vào file `answer.md` được chỉ định.
3. Chạy: `python "<PD_Bridge>\tools\bridge.py" finish Q007` → ghi `cau_hoi/Q007_traloi.md` chuẩn định dạng.

Lệnh hữu ích khác: `bridge.py status` (hàng đợi, hạn mức, chỉ mục), `bridge.py doctor` (kiểm tra cài đặt),
`bridge.py weekly --force` (tổng hợp tuần ngay), `bridge.py stop` (dừng watcher).

## 3. Công cụ cục bộ cho việc research (rẻ token)

| Lệnh | Tác dụng |
|---|---|
| `python tools/pd_index.py search "hold uncertainty ccopt" [--kind doc] [-n 20]` | tìm toàn văn (PDF theo trang, text theo dải dòng) |
| `python tools/pd_index.py show "<path>" --page 1234` | đọc đúng 1 trang PDF từ chỉ mục |
| `python tools/pd_index.py show "<path>" --grep "set_clock_uncertainty" -C 3` | grep file lớn, kèm số dòng |
| `python tools/pd_summarize.py "<report|log|thư mục run>"` | tóm tắt WNS/TNS, top path, ERROR/WARN theo mã… |

## 4. File và vị trí

| Vị trí | Nội dung |
|---|---|
| `<OneDrive - Alchip…>\PD_Bridge\` | câu hỏi/trả lời, tổng hợp, kiến thức, tools (đồng bộ về máy người hỏi) |
| `%LOCALAPPDATA%\PD_Bridge\config.json` | cấu hình (models, data_dir, runner…) |
| `%LOCALAPPDATA%\PD_Bridge\watcher.log`, `runs\` | nhật ký, prompt/kết quả từng lượt |
| `<physical design>\.pd_index\index.db` | chỉ mục SQLite FTS5 |
