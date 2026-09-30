# PhysicalDesign

## PD_Bridge — hỏi đáp Physical Design tự động

Thư mục [`PD_Bridge/`](PD_Bridge/): bạn thả file câu hỏi vào `cau_hoi/` (OneDrive Alchip).
Watcher trên máy ngoài phát hiện file mới (0 token), gọi Claude Code tra dữ liệu trong
thư mục **physical design** và research web, rồi ghi `Qnnn_traloi.md` vào cùng thư mục.

- Hướng dẫn sử dụng và cài đặt: [`PD_Bridge/HUONG_DAN.md`](PD_Bridge/HUONG_DAN.md)
- Quy trình cho Claude / bảo trì: [`PD_Bridge/_QUY_TRINH_CLAUDE.md`](PD_Bridge/_QUY_TRINH_CLAUDE.md)
- Kiểm thử (Linux/macOS/Windows, không tốn token): `python3 -m unittest tests/test_bridge.py -v`
