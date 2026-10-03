# PD_Bridge — hỏi đáp Physical Design tự động

Bạn chỉ cần tạo một file câu hỏi. Mọi việc còn lại tự động:

1. Watcher trên máy ngoài phát hiện file mới. Bước này không tốn token.
2. Watcher bật Claude Code ngay trên máy đó. Claude tự tìm dữ liệu trong thư mục **physical design** (report, log, script, tài liệu) rồi research thêm trên web.
3. Câu trả lời `Qnnn_traloi.md` được ghi vào thư mục câu hỏi, rồi OneDrive (tài khoản cá nhân `khuongmat456@gmail.com`) đồng bộ về máy bạn.

Hệ thống **chỉ chạy khi bạn bật**. Khi tắt, máy ngoài không tốn tài nguyên gì.

## Bật / tắt hệ thống (trên máy ngoài)

| Việc | Gõ lệnh (cmd hoặc PowerShell) | Hoặc double-click trên Desktop |
|---|---|---|
| **Bật** (chạy nền, ẩn; nhận câu hỏi) | `pdbat` | **PD_Bridge - BAT** |
| **Tắt hẳn** | `pdtat` | **PD_Bridge - TAT** |
| Xem đang bật hay tắt, hàng đợi | `pdtt` | **PD_Bridge - TRANG THAI** |
| Kiểm tra cài đặt, đăng nhập | `pd doctor` | — |
| Trả lời các câu đang chờ rồi thoát (không chạy nền) | `pd once` | — |

- **Mặc định không tự chạy khi mở máy.** Mỗi lần cần dùng thì gõ `pdbat`; dùng xong gõ `pdtat`.
- `pdtat` tắt ngay, kể cả khi Claude đang trả lời dở. File trả lời ghi "⏸ Hệ thống đã TẮT khi đang trả lời", và câu đó được trả lời lại từ đầu ở lần bật sau.
- Câu hỏi gửi lúc hệ thống tắt vẫn nằm trong OneDrive. Khi bật, chúng được trả lời theo thứ tự.
- Đã tắt là tắt hẳn: không tự bật lại, kể cả khi khởi động lại máy, cho tới khi bạn gõ `pdbat`.
- Từ xa (máy bạn) xem `cau_hoi/_TRANG_THAI.md` trên OneDrive. Khi hệ thống tắt, file ghi "⏹ ĐÃ TẮT lúc …".
- Muốn tự bật mỗi lần đăng nhập Windows: chạy lại `cai_dat.ps1 -AutoStart`. Muốn bỏ: chạy lại `cai_dat.ps1` (không kèm `-AutoStart`).
- Muốn tự tắt khi rảnh: đặt `"idle_stop_minutes": 60` trong `%LOCALAPPDATA%\PD_Bridge\config.json`. Hệ thống sẽ tự tắt sau 60 phút không có câu hỏi; mặc định là 0 (không tự tắt).

**Tài nguyên dùng:**

| Trạng thái | Tài nguyên |
|---|---|
| **Tắt** | 0: không có tiến trình nào, máy ngủ bình thường |
| **Bật, đang rảnh** | Một tiến trình Python nhỏ (khoảng 30–50 MB RAM, gần 0% CPU), quét thư mục mỗi 1 giây, rảnh quá 10 phút thì mỗi 3 giây. Máy không tự ngủ trong lúc bật |
| **Bật, đang trả lời** | Thêm Claude Code (khoảng 200–400 MB RAM) cho mỗi câu, tối đa 2 câu song song |

## Cách hỏi

1. Trong `cau_hoi/`, tạo một file `.md` (hoặc `.txt`). **Tên file quyết định mode và model**:

   | Tên file | Mode | Model |
   |---|---|---|
   | `[nhanh] cu phap set_clock_uncertainty.md` | nhanh | Sonnet |
   | `hold sau cts.md` (không có tiền tố) | chuan | Opus |
   | `[sau] vi sao hold am sau cts.md` | sau | Opus |
   | `[sau-sonnet] ….md`, `[nhanh-opus] ….md`, `[haiku] ….md` | theo tiền tố | model ghi sau dấu `-` |

   Không cần mẫu. Chỉ cần viết câu hỏi và yêu cầu vào file. Dòng `mode: …` ở đầu nội dung vẫn được nhận, và ưu tiên hơn tiền tố trong tên file.
2. **Gửi kèm dữ liệu** (report, log, script, ảnh, PDF). Có 2 cách:
   - **Thư mục**: tạo thư mục `cau_hoi/[sau] hold median/`, bỏ vào đó 1 file câu hỏi `.md` cùng các file dữ liệu.
   - **Cùng tên gốc**: `hold.md` kèm `hold_timing.rpt`, `hold_innovus.log`.

   Script tự tóm tắt report/log (WNS/TNS, top path, ERROR/WARN…) và đưa vào đề bài, nên Claude không tốn token mở file lớn. Dữ liệu được chuyển ra **kho trên máy ngoài** (không còn nằm trên OneDrive) và lưu vĩnh viễn.
2b. **Chọn project** (tuỳ chọn): thêm `@ten_project` vào tên file (ví dụ `[sau] @median_filter hold.md`) hoặc dòng `du_an: median_filter` trong nội dung. Không ghi gì thì script tự nhận theo tên block xuất hiện trong câu hỏi (so với các thư mục trong physical design); không khớp thì vào project `chung`.
3. Lưu file là xong. **Khoảng 2–3 giây** sau khi file có trên máy ngoài, `Qnnn_traloi.md` xuất hiện với trạng thái "⏳ Đã nhận" và Claude bắt đầu làm. Tên câu hỏi được đổi thành `Qnnn_<tên của bạn>.md`.
   - Đo thực tế: phát hiện file và hiện "⏳" sau 2–4 giây.
     - Mode `nhanh` (Sonnet): xong sau 1–2 phút.
     - Mode `chuan` (Opus, suy nghĩ rất kỹ, **có lượt rà soát**): hiện bản nháp Kết luận sau khoảng 5 phút, xong sau khoảng **10–15 phút**. Lần đo thật: 15 phút, câu trả lời 52 KB, lượt rà soát tự sửa 3 chỗ và tính lại toàn bộ số liệu.
     - Mode `sau`: có thể 30–60 phút.
   - Cần nhanh thì dùng `[nhanh]`. Muốn `chuan` nhanh hơn thì tắt lượt rà soát: đặt `"review_modes": ["sau"]` trong `config.json`.
   - Tối đa **2 câu chạy song song**, nên câu `nhanh` không phải chờ câu `sau` đang chạy.
   - Còn thêm thời gian OneDrive đồng bộ giữa hai máy (thường 5–30 giây). Phần này do OneDrive quyết định, script không điều khiển được.

Claude tự tìm report, log, script và tài liệu (kể cả Innovus Text Command Reference) trong thư mục physical design, rồi kiểm chứng và bổ sung bằng web.

| mode | Dùng khi | Cách làm | Model mặc định |
|---|---|---|---|
| `nhanh` | Câu hỏi lệnh/cú pháp, cần kết quả sớm | Kho kinh nghiệm và tài liệu trên máy, web để kiểm chứng | Sonnet |
| `chuan` (mặc định) | Phần lớn câu hỏi | Tìm kỹ trên máy, đọc dữ liệu liên quan, research web 2–4 nguồn | Opus |
| `sau` | Debug khó, so sánh phương án, cần lý thuyết (có thể 20–40 phút) | Nhiều vòng trên máy và web (paper, app note), so sánh phương án | Opus |

Mọi mode đều cho câu trả lời đầy đủ, không giới hạn độ dài. Mode chỉ quyết định mức độ research.
Viết `mode: sâu`, `Mode = Nhanh`… cũng được nhận.

**Hai lượt cho mode chuan và sâu:**
1. **Lượt 1 — nghiên cứu và viết:** Claude tra dữ liệu, tài liệu tool và web, rồi viết đủ các mục.
2. **Lượt 2 — rà soát và hoàn thiện:** Claude đóng vai reviewer PD cấp cao, khắt khe, rồi kiểm lại từng điểm:
   - đã trả lời đủ từng ý chưa;
   - mỗi lệnh và tuỳ chọn có trong tài liệu không;
   - mỗi con số có khớp nguồn không, phép tính có đúng không;
   - phần "Áp dụng ngay" đã chạy được ngay chưa;
   - còn thiếu rủi ro hay ảnh hưởng chéo nào không.

   Claude sửa thẳng vào câu trả lời và ghi lại những gì đã sửa ở mục `## 🔎 Đã rà soát`. Nếu lượt rà soát lỗi, hệ thống giữ nguyên bản đầu.

**Bố cục file trả lời `Qnnn_traloi.md`** — luôn đọc từ trên xuống, phần mới nhất ở đầu:

1. `## ❓ Câu hỏi` / `## ❓ Hỏi tiếp`: câu hỏi của lượt mới nhất.
2. `## ✅ Kết luận`: trả lời thẳng từng ý (1), (2)… kèm số liệu.
3. `## 🛠 Áp dụng ngay`: các bước đánh số, lệnh Tcl chạy được, giá trị cụ thể, **cách kiểm tra kết quả và con số kỳ vọng**, dấu hiệu sai và cách quay lui.
4. `## 🔍 Giải thích chi tiết`, `## ⚖️ Phương án & so sánh`, `## ⚠️ Lỗi thường gặp`, `## Nguồn`, `## Độ tin cậy`.
5. `## Đã trả lời đủ chưa?`: Claude tự đối chiếu từng ý của câu hỏi.
6. `## Bài học` và bảng `Kiểm chứng tự động`: 📘 có trong tài liệu tool, 📄 chỉ thấy trong script dự án, ⚠️ chưa kiểm chứng (nên chạy `help <lệnh>`).
7. **📜 Các lượt trước (n)**: các lượt hỏi đáp cũ được **thu gọn**, bấm để mở.
8. Phần đánh giá ở cuối file.

Trong lúc Claude làm, đầu file hiện **⏳ tiến độ** (cập nhật mỗi 10 giây). Khi Claude đã viết xong phần Kết luận, file hiện thêm **📝 bản nháp** để bạn đọc trước.

**Hỏi tiếp:** mở `Qnnn_traloi.md`, viết một hoặc nhiều dòng bắt đầu bằng `>>` (hoặc `»`, ký tự mà bàn phím tiếng Việt hay tự đổi từ `>>`) ở cuối file, rồi lưu. Ví dụ: `>> Nếu vẫn còn path âm thì sao?`. Muốn chọn mode cho riêng lượt này thì viết `>> [nhanh] …` hoặc `>> [sau] …`. Claude nhận được toàn bộ chuỗi hỏi đáp trước đó và tiếp tục đúng phiên cũ, nên giữ được mạch suy nghĩ: câu trả lời nói rõ điều gì giữ nguyên, điều gì thay đổi so với lượt trước. Lượt mới được đặt lên đầu file, lượt cũ tự thu gọn. Câu hỏi tiếp không bị mất kể cả khi bạn gõ thêm `>>` trong lúc Claude đang trả lời, hoặc khi máy bị tắt giữa chừng.

**Sửa câu hỏi:** mở file câu hỏi **đã được đổi tên** (`Qnnn_...md`), sửa hoặc thêm yêu cầu, rồi lưu. Claude trả lời lại và tận dụng bản cũ: chỉ research phần mới và sửa chỗ bạn đánh giá sai. Bản cũ được giữ ở `Qnnn_traloi_cu.md`.

**Đánh giá:** ở cuối file trả lời, sửa `danh_gia: chua` thành `dung`, `mot_phan` hoặc `sai`, và thêm ghi chú sau `ghi_chu:` nếu cần. Việc sửa file trả lời không làm Claude chạy lại. Chỉ điều bạn xác nhận đúng mới trở thành kiến thức ✅.

**Tổng hợp tuần:** sau 16:55 thứ Sáu, Claude viết `tong_hop/<năm>-W<tuần>.md` và cập nhật `kien_thuc/kinh_nghiem.md`. Nếu lúc đó máy tắt thì khi bật lên sẽ làm bù. Tuần không có câu hỏi thì không tốn token.

## Kho project, bộ nhớ và học tập

**Kho project nằm trên máy ngoài** (mặc định `%USERPROFILE%\PD_Bridge_Kho`), không đồng bộ lên OneDrive:

    PD_Bridge_Kho\du_an\<project>\
    ├── hoi_dap\        mọi câu hỏi + trả lời, lưu VĨNH VIỄN (Qnnn_traloi.md, các bản cũ Qnnn_traloi_v1.md…)
    ├── du_lieu\        report/log/script bạn gửi kèm, theo từng Qnnn
    ├── BOI_CANH.md     bối cảnh project (sửa tay: block, flow, phiên bản tool, mục tiêu…) — Claude đọc ở mọi câu
    ├── KIEN_THUC.md    kiến thức project; mục ◻️ "chưa xác nhận" do script tự thêm
    ├── MUC_LUC.md      bảng mọi câu hỏi: ngày, chủ đề, đánh giá, kết luận 1 dòng (tự sinh)
    └── DONG_THOI_GIAN.md  chỉ số chính mỗi lần gửi report (WNS/TNS, ERROR…) — thấy block tốt lên/xấu đi

Trên OneDrive, `cau_hoi/` chỉ còn là hộp thư (giữ 5 lượt gần nhất). Bản đầy đủ luôn nằm trong kho.

**Mỗi câu hỏi được trả lời MỚI và độc lập:** mặc định hệ thống **không** đưa hỏi đáp cũ của câu khác vào đề bài, nên Claude không lặp lại câu trả lời cũ mà nghiên cứu và kiểm chứng lại từ đầu. Muốn bật lại thì đặt `"use_history": true` trong `config.json`.

**Thông tin đưa vào mỗi câu hỏi** (script làm, 0 token):

| Bộ nhớ | Tác dụng |
|---|---|
| Trích sẵn tài liệu tool | Nội dung đúng trang/đoạn khớp nhất trong Text Command Reference trên máy, để Claude có căn cứ chính xác ngay từ đầu |
| Dữ liệu bạn đã gửi | Tìm toàn văn trong report/log/script bạn từng gửi kèm (ưu tiên project hiện tại); không lấy câu trả lời cũ |
| Bối cảnh + kiến thức | `BOI_CANH.md` và `KIEN_THUC.md` của project, `kien_thuc/` chung (✅ đã xác nhận, ◻️ chưa xác nhận) |
| Từ điển đồng nghĩa | Thuật ngữ Việt → Anh (độ lệch → skew, xung nhịp → clock, nhiễu xuyên âm → crosstalk…) và viết tắt (CTS → ccopt, OCV → AOCV/POCV/derate, DRV → max_tran/max_cap…), giúp tìm đúng tài liệu tiếng Anh. Tìm được cả theo tên block và tên lệnh |

**Tự học (tắt mặc định, bật bằng `use_history`):** câu trả lời sau 14 ngày mà không bị đánh giá `sai` được script tự thêm vào mục ◻️ **Chưa xác nhận** trong `KIEN_THUC.md` của project. Claude vẫn dùng lại được các mục này nhưng phải kiểm chứng lại. Nếu sau đó bạn đánh giá `sai` thì mục tự bị gỡ; đánh giá `dung` thì thành ✅ ở lượt tổng hợp tuần.

**Sổ tay (trên OneDrive, để đọc học):** `so_tay/so_lenh.md` gom mọi lệnh Innovus/Tcl, xếp theo nhóm lệnh. `so_tay/thuat_ngu.md` gom các thuật ngữ. Cả hai lấy từ mục Bài học của các câu trả lời, 0 token. Mỗi mục có ký hiệu:
- ✅: câu nguồn đã được bạn xác nhận đúng.
- ◻️: chưa xác nhận.
- 📘 hoặc ⚠️: có hoặc chưa thấy trong tài liệu tool.
- Kèm danh sách Qnnn nguồn để mở lại.

## Tốn token thế nào

- **Khi không có câu hỏi: 0 token.** Việc phát hiện file mới (quét mỗi giây), làm mới chỉ mục tài liệu, dọn file cũ và kiểm tra đăng nhập đều do script trên máy làm.
- **Mỗi câu hỏi:** một lần chạy Claude Code, tính vào hạn mức gói Claude của bạn. **Không dùng API và không phát sinh chi phí ngoài:** watcher gỡ `ANTHROPIC_API_KEY` khỏi môi trường, và từ chối chạy nếu Claude Code đang đăng nhập bằng API key.
- **Tiết kiệm phần đọc:**
  - Script trên máy tìm sẵn tài liệu liên quan và đưa vào đề bài.
  - Report 150KB được tóm tắt còn vài chục dòng.
  - PDF lớn được đọc đúng trang cần.
  - Claude Code chỉ nạp các công cụ cần dùng và không nạp MCP server. Nhờ vậy phần token cố định mỗi bước giảm từ khoảng 29.000 xuống 11.000 (đo thực tế).
  - Câu trả lời ghi thẳng vào file, không in lặp ra màn hình.
  - Câu trả lời thì không bao giờ bị cắt ngắn.
- **Khi hết hạn mức:** nếu Opus hết thì watcher chuyển sang Sonnet ngay. Nếu cả gói hết thì watcher đọc giờ reset, tạm dừng, rồi tự làm tiếp. Trong lúc đó câu hỏi mới vẫn được nhận và xếp hàng.
- **Đổi model:** sửa mục `models` trong `%LOCALAPPDATA%\PD_Bridge\config.json`. Ví dụ đặt `"chuan": "sonnet"` để được nhiều câu hỏi hơn mỗi ngày. Mục `effort` chỉnh độ suy nghĩ (`low` / `medium` / `high`).

## Thiết lập (một lần, trên máy ngoài)

| Bước | Việc |
|---|---|
| 0 | Chép thư mục `PD_Bridge` (trong repo này) vào `C:\Users\khiem\OneDrive\` |
| 1 | Trên máy ngoài, đăng nhập ứng dụng OneDrive bằng tài khoản **khuongmat456@gmail.com**, rồi chờ đồng bộ xong |
| 2 | Mở PowerShell và chạy: `powershell -ExecutionPolicy Bypass -File "C:\Users\khiem\OneDrive\PD_Bridge\tools\cai_dat.ps1"` |
| 3 | Trả lời **Y** các câu script hỏi: chuyển PD_Bridge vào OneDrive của khuongmat456@gmail.com (nếu đang ở chỗ khác), cài Python / Git / Claude Code nếu thiếu. Trình duyệt sẽ mở để bạn đăng nhập Claude (tài khoản Pro của bạn) |
| 4 | Script tạo lệnh `pdbat` / `pdtat` / `pdtt` và 3 shortcut trên Desktop, rồi bật hệ thống một lần để chạy thử. Từ đó, mỗi lần cần dùng thì gõ `pdbat` (lệnh có hiệu lực ở cửa sổ cmd/PowerShell mở **mới**) |

- Script tự tìm thư mục OneDrive của tài khoản `khuongmat456@gmail.com` (thường là `C:\Users\<tên>\OneDrive`). Muốn dùng tài khoản khác thì thêm `-OneDriveAccount <email>`.
- Thư mục dữ liệu mặc định là `D:\K\K\physical design`. Nếu không thấy, script tự tìm thư mục tên `physical design` hoặc hỏi bạn. Muốn chỉ định thì thêm `-DataDir "…"`.
- Ngay sau bước 3, câu hỏi thử `cts skew uncertainty.md` đang chờ sẽ được trả lời. Đó là bài kiểm tra toàn tuyến; nếu không cần, xoá file đó trước khi chạy.
- Cấu hình và nhật ký nằm ở `%LOCALAPPDATA%\PD_Bridge`, không nằm trong OneDrive. Lệnh `pdbat`/`pdtat`/`pdtt`/`pd` nằm ở `%LOCALAPPDATA%\PD_Bridge\bin` (đã thêm vào PATH). Gỡ: chạy `cai_dat.ps1 -Uninstall`.

## Quyền cần cấp để hệ thống tự chạy

| # | Quyền | Ở đâu | Bắt buộc? |
|---|---|---|---|
| 1 | Đăng nhập tài khoản **OneDrive khuongmat456@gmail.com** trên máy ngoài, thư mục PD_Bridge ở chế độ "Always keep on this device" (script tự đặt) | Ứng dụng OneDrive trên máy ngoài | Bắt buộc |
| 2 | Chạy script PowerShell một lần: `-ExecutionPolicy Bypass` (không cần quyền admin) | PowerShell | Bắt buộc |
| 3 | Đăng nhập **Claude Code** bằng tài khoản Claude (Pro/Max), không dùng API key | Trình duyệt mở ra khi chạy `cai_dat.ps1` | Bắt buộc |
| 4 | Thêm `%LOCALAPPDATA%\PD_Bridge\bin` vào PATH của user và tạo shortcut Desktop (script tự làm). Task Scheduler chỉ dùng khi chọn `-AutoStart` | Windows | Tự động |
| 5 | Cài Python, Git for Windows, Claude Code qua winget hoặc trình cài chính thức | Windows | Chỉ khi máy chưa có |
| 6 | Đọc thư mục `D:\K\K\physical design` (Claude chỉ đọc; quyền ghi bị chặn ở cấp permission) | Máy ngoài | Bắt buộc |
| 6b | Tạo thư mục kho `%USERPROFILE%\PD_Bridge_Kho` (script tự tạo; đổi chỗ bằng `-StoreDir`) | Máy ngoài | Tự động |
| 7 | Đổi cài đặt nguồn: gập nắp khi cắm sạc thì không ngủ | `powercfg` (script hỏi) | Nên có |
| 8 | Quyền ghi GitHub cho Claude (đã có) để cập nhật code PD_Bridge | github.com/apps/claude | Để cập nhật tool |

Trong lúc chạy, Claude Code trên máy ngoài **không hỏi xác nhận gì**: các quyền được cấp sẵn trong lệnh gọi, gồm đọc dữ liệu, web, chạy script tra cứu cục bộ và ghi file trả lời. Mọi thao tác khác đều bị từ chối tự động.

## Cấu trúc

    PD_Bridge/
    ├── HUONG_DAN.md · _QUY_TRINH_CLAUDE.md · CLAUDE.md
    ├── cau_hoi/     hộp thư: câu hỏi và trả lời (giữ 5 lượt mới nhất; bản đầy đủ ở kho máy ngoài)
    ├── tong_hop/    nhật ký (nhat_ky.md, nhat_ky.jsonl) và tổng hợp tuần
    ├── kien_thuc/   kinh_nghiem.md (kiến thức ✅ tích luỹ), boi_canh_du_an.md (bối cảnh dự án)
    ├── so_tay/      so_lenh.md, thuat_ngu.md (tự gom từ mục Bài học)
    └── tools/       watcher.ps1, cai_dat.ps1, bridge.py, pd_index.py, pd_summarize.py, pd_store.py

Chỉ mục tìm kiếm nằm ở `D:\K\K\physical design\.pd_index\index.db`. Nó chứa toàn văn PDF (theo trang), HTML, docx, script, report và log. Watcher làm mới chỉ mục mỗi ngày, chỉ đọc lại những file đã thay đổi. Xem số file bằng lệnh `status` bên dưới.

`kien_thuc/boi_canh_du_an.md` được đưa vào mọi câu hỏi. Hãy ghi ngắn gọn tên block, flow và phiên bản tool để câu trả lời sát dự án hơn.

## Bảng lệnh (0 token)

| Lệnh ngắn | Lệnh đầy đủ | Tác dụng |
|---|---|---|
| `pdbat` | `python "<PD_Bridge>\tools\bridge.py" start` | Bật hệ thống (chạy nền, ẩn). Đang bật rồi thì không bật thêm |
| `pdtat` | `python "<PD_Bridge>\tools\bridge.py" stop` | Tắt hẳn, kể cả câu đang trả lời dở (được làm lại khi bật) |
| `pdtt` | `python "<PD_Bridge>\tools\bridge.py" status` | 🟢 ĐANG BẬT / ⏹ ĐÃ TẮT, hàng đợi, chỉ mục, đánh giá |
| `pd doctor` | `… bridge.py doctor` | Kiểm tra cài đặt, đăng nhập Claude |
| `pd once` | `… bridge.py once` | Trả lời hết các câu đang chờ rồi thoát (không chạy nền) |
| `pd weekly --force` | `… bridge.py weekly --force` | Tổng hợp tuần ngay |
| `pd watch` | `… bridge.py watch` | Chạy ở cửa sổ hiện tại để xem trực tiếp (Ctrl+C để dừng) |

Nếu gõ `pdbat` mà báo "not recognized", hãy mở cửa sổ mới. Nếu vẫn không được, dùng lệnh đầy đủ, hoặc chạy lại `cai_dat.ps1`.

## Sự cố thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| File trả lời đứng ở "⏳ Đang xử lý" | Xem file: tiến độ cập nhật mỗi 10 giây (thời gian, số bước, 5 bước gần nhất). Nếu Claude im lặng quá **5 phút**, watcher tự dừng lượt đó và chạy lại (tối đa 8 lần, rồi báo lỗi rõ ràng) |
| Gửi câu hỏi mà không thấy "⏳ Đã nhận" | Hệ thống đang tắt. Trên máy ngoài gõ `pdbat` (hoặc double-click **PD_Bridge - BAT**) |
| Muốn biết máy ngoài đang ra sao | Mở `cau_hoi/_TRANG_THAI.md` (ngay trên OneDrive, cập nhật mỗi phút khi bật): bật/tắt, đăng nhập Claude, việc đang làm, lỗi gần nhất, nhật ký. Không ghi "ĐÃ TẮT" mà giờ cập nhật đã cũ nghĩa là máy ngoài đã tắt/ngủ |
| "⚠️ Chưa chạy được: … CHƯA ĐĂNG NHẬP" | Trên máy ngoài chạy `claude auth login`. Watcher tự thử lại mỗi 10 phút |
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
