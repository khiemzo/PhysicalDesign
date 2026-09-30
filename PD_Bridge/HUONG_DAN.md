# PD_Bridge — hỏi đáp Physical Design tự động

Bạn chỉ cần tạo một file câu hỏi. Mọi việc còn lại tự động:

1. Watcher trên máy ngoài phát hiện file mới. Bước này không tốn token.
2. Watcher bật Claude Code ngay trên máy đó. Claude tự tìm dữ liệu trong thư mục **physical design** (report, log, script, tài liệu) rồi research thêm trên web.
3. Câu trả lời `Qnnn_traloi.md` được ghi vào thư mục câu hỏi, rồi OneDrive Alchip đồng bộ về máy bạn.

## Cách hỏi

1. Trong `cau_hoi/`, tạo một file `.md` (hoặc `.txt`), đặt tên tuỳ ý, ví dụ `hold sau cts.md`.
2. Viết câu hỏi và yêu cầu. Muốn chọn mode thì thêm một dòng ở đầu file:

       mode: sau
       Vì sao hold âm nhiều sau CTS ở block median_filter? So sánh với flow trong one.tcl.

3. Lưu file là xong. Khoảng 1 phút sau khi file dừng thay đổi, Claude bắt đầu làm.
   - File câu hỏi được đổi tên thành `Qnnn_<tên của bạn>.md`.
   - `Qnnn_traloi.md` xuất hiện ngay với trạng thái ⏳, xong thì được thay bằng câu trả lời.

Không cần mẫu và không cần chỉ đường dẫn dữ liệu. Claude tự tìm report, log, script và tài liệu (kể cả Innovus Text Command Reference) trong thư mục physical design, rồi kiểm chứng và bổ sung bằng web.

| mode | Dùng khi | Cách làm | Model |
|---|---|---|---|
| `nhanh` | Câu hỏi lệnh/cú pháp, cần kết quả sớm (vài phút) | Kho kinh nghiệm và tài liệu trên máy, web để kiểm chứng | Sonnet |
| `chuan` (mặc định) | Phần lớn câu hỏi | Tìm kỹ trên máy, đọc dữ liệu liên quan, research web 2–4 nguồn | Opus |
| `sau` | Debug khó, so sánh phương án, cần lý thuyết (có thể 20–40 phút) | Nhiều vòng trên máy và web (paper, app note), so sánh phương án | Opus |

Mọi mode đều cho câu trả lời đầy đủ, không giới hạn độ dài. Mode chỉ quyết định mức độ research.
Viết `mode: sâu`, `Mode = Nhanh`… cũng được nhận.

Mỗi câu trả lời có các phần: **Tóm tắt**, **Trả lời chi tiết** (có số liệu từ report), **Lệnh / script đề xuất**, **Nguồn** (đường dẫn trên máy kèm trang hoặc dòng, và URL web), **Độ tin cậy & cần kiểm chứng**.

**Sửa câu hỏi:** mở file câu hỏi **đã được đổi tên** (`Qnnn_...md`), sửa hoặc thêm yêu cầu, rồi lưu. Claude trả lời lại và tận dụng bản cũ: chỉ research phần mới và sửa chỗ bạn đánh giá sai. Bản cũ được giữ ở `Qnnn_traloi_cu.md`.

**Đánh giá:** ở cuối file trả lời, sửa `danh_gia: chua` thành `dung`, `mot_phan` hoặc `sai`, và thêm ghi chú sau `ghi_chu:` nếu cần. Việc sửa file trả lời không làm Claude chạy lại. Chỉ điều bạn xác nhận đúng mới trở thành kiến thức ✅.

**Tổng hợp tuần:** sau 16:55 thứ Sáu, Claude viết `tong_hop/<năm>-W<tuần>.md` và cập nhật `kien_thuc/kinh_nghiem.md`. Nếu lúc đó máy tắt thì khi bật lên sẽ làm bù. Tuần không có câu hỏi thì không tốn token.

## Tốn token thế nào

- **Khi không có câu hỏi: 0 token.** Việc phát hiện file mới (quét mỗi 5 giây), làm mới chỉ mục tài liệu, dọn file cũ và kiểm tra đăng nhập đều do script trên máy làm.
- **Mỗi câu hỏi:** một lần chạy Claude Code, tính vào hạn mức gói Claude của bạn. **Không dùng API và không phát sinh chi phí ngoài:** watcher gỡ `ANTHROPIC_API_KEY` khỏi môi trường, và từ chối chạy nếu Claude Code đang đăng nhập bằng API key.
- **Tiết kiệm phần đọc:**
  - Script trên máy tìm sẵn tài liệu liên quan và đưa vào đề bài.
  - Report 150KB được tóm tắt còn vài chục dòng.
  - PDF lớn được đọc đúng trang cần.
  - Claude Code không nạp MCP server, và câu trả lời không bị in lặp ra màn hình.
  - Câu trả lời thì không bao giờ bị cắt ngắn.
- **Khi hết hạn mức:** nếu Opus hết thì watcher chuyển sang Sonnet ngay. Nếu cả gói hết thì watcher đọc giờ reset, tạm dừng, rồi tự làm tiếp. Trong lúc đó câu hỏi mới vẫn được nhận và xếp hàng.
- **Đổi model:** sửa mục `models` trong `%LOCALAPPDATA%\PD_Bridge\config.json`. Ví dụ đặt `"chuan": "sonnet"` để được nhiều câu hỏi hơn mỗi ngày. Mục `effort` chỉnh độ suy nghĩ (`low` / `medium` / `high`).

## Thiết lập (một lần, trên máy ngoài)

| Bước | Việc |
|---|---|
| 0 | Chép thư mục `PD_Bridge` (trong repo này) vào `C:\Users\khiem\OneDrive\` |
| 1 | Thêm tài khoản OneDrive **Alchip**: bấm biểu tượng OneDrive → Settings → Account → Add an account, rồi chờ đồng bộ xong |
| 2 | Mở PowerShell và chạy: `powershell -ExecutionPolicy Bypass -File "C:\Users\khiem\OneDrive\PD_Bridge\tools\cai_dat.ps1"` |
| 3 | Trả lời **Y** các câu script hỏi: chuyển PD_Bridge sang OneDrive Alchip, cài Python / Git / Claude Code nếu thiếu. Trình duyệt sẽ mở để bạn đăng nhập Claude (tài khoản Pro của bạn) |
| 4 | Để máy bật và đăng nhập Windows, đừng gập nắp laptop. Khi đang chạy, watcher tự chặn máy ngủ |

- Script tự tìm thư mục `OneDrive - Alchip…`. Nếu chưa thấy (chưa làm bước 1), PD_Bridge tạm ở OneDrive cá nhân; chạy lại script sau khi thêm tài khoản.
- Thư mục dữ liệu mặc định là `D:\K\K\physical design`. Nếu không thấy, script tự tìm thư mục tên `physical design` hoặc hỏi bạn. Muốn chỉ định thì thêm `-DataDir "…"`.
- Ngay sau bước 3, câu hỏi thử `cts skew uncertainty.md` đang chờ sẽ được trả lời. Đó là bài kiểm tra toàn tuyến; nếu không cần, xoá file đó trước khi chạy.
- Cấu hình và nhật ký nằm ở `%LOCALAPPDATA%\PD_Bridge`, không nằm trong OneDrive. Gỡ watcher: chạy `cai_dat.ps1 -Uninstall`.

## Cấu trúc

    PD_Bridge/
    ├── HUONG_DAN.md · _QUY_TRINH_CLAUDE.md · CLAUDE.md
    ├── cau_hoi/     câu hỏi và trả lời (giữ 5 lượt mới nhất)
    ├── tong_hop/    nhật ký (nhat_ky.md, nhat_ky.jsonl) và tổng hợp tuần
    ├── kien_thuc/   kinh_nghiem.md (kiến thức ✅ tích luỹ), boi_canh_du_an.md (bối cảnh dự án)
    └── tools/       watcher.ps1, cai_dat.ps1, bridge.py, pd_index.py, pd_summarize.py

Chỉ mục tìm kiếm nằm ở `D:\K\K\physical design\.pd_index\index.db`. Nó chứa toàn văn PDF (theo trang), HTML, docx, script, report và log. Watcher làm mới chỉ mục mỗi ngày, chỉ đọc lại những file đã thay đổi. Xem số file bằng lệnh `status` bên dưới.

`kien_thuc/boi_canh_du_an.md` được đưa vào mọi câu hỏi. Hãy ghi ngắn gọn tên block, flow và phiên bản tool để câu trả lời sát dự án hơn.

## Lệnh kiểm tra (chạy trong PowerShell, 0 token)

    python "<PD_Bridge>\tools\bridge.py" status     # hàng đợi, hạn mức, chỉ mục
    python "<PD_Bridge>\tools\bridge.py" doctor     # kiểm tra cài đặt, đăng nhập
    python "<PD_Bridge>\tools\bridge.py" stop       # dừng watcher (Task Scheduler sẽ bật lại lần đăng nhập sau)

## Sự cố thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| Không có câu trả lời sau ~10 phút | Xem `%LOCALAPPDATA%\PD_Bridge\watcher.log`. Kết quả từng lượt (prompt, output) nằm trong thư mục `runs\` cạnh đó |
| Log ghi "chua dang nhap" | Mở PowerShell, gõ `claude` rồi đăng nhập lại (hoặc `claude auth login`) |
| Log ghi "Loi (limit)" | Đã hết hạn mức gói. Watcher tự chạy lại khi reset; file trả lời ghi giờ chạy lại |
| Log ghi "dang dung API key" | Claude Code đang đăng nhập bằng API key. Chạy `claude auth logout` rồi `claude auth login` bằng tài khoản Claude |
| Trả lời ghi "❌ Không tạo được câu trả lời" | Đã thử 3 lần vẫn lỗi. Xem chi tiết trong file, sửa hoặc thêm một dòng trong câu hỏi rồi lưu để thử lại |
| Muốn trả lời ngay khi đang chat | Nhắn **làm** trong chat đã liên kết với máy (quy trình trong `_QUY_TRINH_CLAUDE.md`) |
| Có thư mục `cau_hoi/_cho_xoa/` | Lượt cũ chờ xoá. Watcher tự xoá sau 1 giờ; nội dung chính đã lưu trong `tong_hop/nhat_ky.jsonl` |
| File tên `abc.md.txt` (Windows ẩn đuôi) | Vẫn được nhận là câu hỏi |
| Notepad lưu lại file theo tên cũ sau khi đã đổi tên | Watcher tự gộp vào câu hỏi `Qnnn` tương ứng, không tạo câu hỏi trùng |
| Đổi thư mục dữ liệu hoặc chuyển máy | Chạy lại `cai_dat.ps1` |

**Dự phòng nếu không cài được Claude Code trên máy ngoài:** có thể cho watcher gọi một routine trên cloud (`cai_dat.ps1 -Runner routine -RoutineUrl … -RoutineToken …`). Routine này phải được tạo tay trong Claude desktop app, bật tuỳ chọn "Require this computer" và có API trigger. Cách này bị giới hạn số lượt mỗi ngày và chưa kiểm chứng đầy đủ, nên chỉ dùng khi cần; nhắn Claude để được hỗ trợ.
