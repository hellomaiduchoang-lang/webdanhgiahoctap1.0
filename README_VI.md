# ClassTrack — hướng dẫn cài đặt, chức năng và báo cáo

## 1. Giới thiệu

ClassTrack là ứng dụng web mã nguồn mở chạy trên Python và SQLite, hỗ trợ giáo viên tạo năm học/lớp, quản lý hồ sơ học sinh, cấp tài khoản bằng mã kích hoạt riêng, chuyển học sinh lên lớp mà không tạo lại hồ sơ, ghi nhận rèn luyện, tạo bài tập và trao đổi qua diễn đàn lớp. Giao diện được thiết kế riêng theo dạng bảng tin; đây không phải bản sao giao diện hay mã nguồn của mạng xã hội khác. Ứng dụng không có phòng live hoặc livestream.

Giao diện dùng bộ màu xanh tím, card và component nhất quán, sidebar có thể thu gọn, thanh điều hướng dưới trên điện thoại, bộ chọn năm/lớp, avatar/menu hồ sơ và chuyển chế độ sáng/tối. Dashboard tổng hợp số liệu từ API/SQLite theo phạm vi quyền; các khu vực chưa có API không hiển thị số liệu mẫu. Icon SVG dùng trực tiếp trong code, không cần cài thư viện icon hay animation mới.

Phiên bản hiện tại ưu tiên luồng lõi và chạy tại máy cá nhân để thử nghiệm. Đây chưa phải dịch vụ triển khai công khai cho dữ liệu thật của trường: ứng dụng không tự tạo lịch sao lưu, không cung cấp TLS, kho tệp riêng tư hoặc quy trình vận hành nhiều máy chủ. Có lệnh sao lưu/khôi phục thủ công để kết hợp với bộ lập lịch của hệ điều hành. Chỉ nhập dữ liệu thử nghiệm cho đến khi trường triển khai hạ tầng và rà soát bảo mật phù hợp.

## 2. Công nghệ và cấu trúc dữ liệu

- Frontend: HTML, CSS, JavaScript thuần, responsive cho máy tính và điện thoại.
- Backend: Python 3.10 trở lên, `http.server` trong thư viện chuẩn.
- Cơ sở dữ liệu: SQLite, tạo tự động tại `classtrack.sqlite3` khi khởi động.
- Mật khẩu: PBKDF2-HMAC-SHA256; mã kích hoạt và session token lưu dưới dạng hash.
- Không cần cài package Python bên ngoài.

Mỗi tài khoản còn có ảnh đại diện tùy chọn. Ảnh được kiểm tra định dạng và lưu trong database ở dạng data URL giới hạn kích thước; ảnh chỉ được trả về cho người đã đăng nhập và có quyền đọc bài viết trong lớp.

Mô hình quan hệ được dùng:

```text
Account 1 ── 1 StudentProfile
StudentProfile 1 ── nhiều ClassMembership
ClassMembership ── Class ── AcademicYear
ConductRecord / Assignment / ForumPost ── Class + AcademicYear
```

`Account` giữ username, hash mật khẩu, trạng thái và nhóm quyền hệ thống. Lớp, nhóm và vai trò cán sự được lưu trong `ClassMembership` theo từng năm. `studentCode` là duy nhất và ổn định. Chuyển năm học tạo membership mới; hồ sơ, tài khoản, mã học sinh, lịch sử năm cũ vẫn giữ nguyên.

## 3. Chạy ứng dụng

1. Cài Python 3.10+ và mở PowerShell tại thư mục dự án.
2. Chạy:

   ```powershell
   python server.py
   ```

3. Mở `http://127.0.0.1:8000`.
4. Nếu database chưa có Super Admin, mở một terminal riêng tại thư mục dự án và chạy `python server.py bootstrap-super-admin`. Nhập tên đăng nhập, email, tên hiển thị và mật khẩu khi được hỏi. Mật khẩu được nhập ẩn. Bootstrap chỉ dùng được một lần.
5. Quay lại cửa sổ máy chủ, đăng nhập bằng tài khoản Super Admin. Mở **Quản trị hệ thống** để gửi lời mời và cấp từng quyền cho quản trị viên khác.
6. Giáo viên đăng nhập, tạo năm học, tạo lớp, rồi thêm hoặc import học sinh.

Máy chủ chỉ nghe trên `127.0.0.1` theo mặc định. Để thử trong mạng nội bộ có thể đặt `CLASSTRACK_HOST=0.0.0.0`, nhưng không nên mở cổng Internet trực tiếp. Muốn dùng qua Internet cần reverse proxy HTTPS, secret/session policy phù hợp, firewall, backup, logging, và rà soát quyền riêng tư trước.

Có thể đổi cổng và đường dẫn database:

```powershell
$env:CLASSTRACK_PORT = "8080"
$env:CLASSTRACK_DB = "D:\ClassTrackData\classtrack.sqlite3"
python server.py
```

### Sao lưu và khôi phục database

Tạo bản sao lưu nhất quán khi ứng dụng đang chạy:

```powershell
python server.py backup
```

Mặc định, tệp `.backup-<UTC timestamp>.bak` được tạo cạnh database. Có thể chỉ định đường dẫn khác; lệnh không ghi đè tệp đã tồn tại:

```powershell
python server.py backup "D:\ClassTrackBackups\classtrack.bak"
```

Để lên lịch sao lưu tự động trên Windows, tạo tác vụ trong **Task Scheduler** chạy `python.exe` với đối số `server.py backup`, đặt **Start in** là thư mục dự án và chọn lịch chạy phù hợp. Dùng tài khoản có quyền ghi vào thư mục sao lưu; bảo vệ bản sao như dữ liệu học sinh và định kỳ thử khôi phục trên một database thử nghiệm.

Khôi phục yêu cầu dừng server trước, sau đó chạy lệnh từ PowerShell tại thư mục dự án:

```powershell
python server.py restore "D:\ClassTrackBackups\classtrack.bak"
```

Lệnh yêu cầu nhập chính xác `RESTORE`, kiểm tra tính toàn vẹn và cấu trúc cơ bản của tệp, tạo bản sao database hiện tại trước khi thay thế. Nếu kiểm tra không đạt, thao tác bị từ chối. Sau khi khôi phục thành công, bản trước khôi phục được giữ cạnh database với hậu tố `.backup-...bak`. Không chạy restore khi server còn hoạt động.

## 4. Quy trình giáo viên và học sinh

### 4.1 Tạo năm học và lớp

Mở mục **Năm học & chuyển lớp**, tạo năm học với tên, ngày bắt đầu/kết thúc và trạng thái `UPCOMING`, `ACTIVE` hoặc `ARCHIVED`. Cơ sở dữ liệu chỉ cho phép một năm học `ACTIVE` cùng lúc. Sau đó tạo lớp trực thuộc năm học. Cùng tên lớp ở hai năm khác nhau được phép.

### 4.2 Thêm học sinh thủ công

Mở **Học sinh & tài khoản**, chọn lớp và có thể dùng bộ lọc năm học ở thanh trên cùng. Giáo viên và quản trị viên có quyền đọc danh sách có thể tải roster đang hiển thị xuống CSV; nội dung xuất tuân theo lớp đã chọn và bộ lọc tìm kiếm hiện tại. Tệp có họ tên, username, mã học sinh, nhóm, trạng thái tài khoản và vai trò lớp, nên cần được lưu/chia sẻ riêng tư. Dùng **Thêm học sinh**, nhập mã học sinh, họ tên và nhóm. Khi tạo, server sinh:

- Một `StudentProfile`.
- Một `Account` trạng thái `PENDING_ACTIVATION` với username được sinh từ mã học sinh (xử lý collision bằng hậu tố).
- Một `ClassMembership` gắn với lớp/năm học.
- Một activation code riêng, ngẫu nhiên, hết hạn sau 30 ngày; database chỉ lưu hash code.

Mã được hiện một lần ngay sau khi tạo. Giáo viên gửi link `/activate` chung trong nhóm lớp và gửi mã cá nhân riêng cho đúng học sinh. Không gửi mật khẩu dùng chung. Nếu mã bị mất, tạo mã mới sẽ vô hiệu hóa mã cũ.

### 4.3 Import CSV/Excel

Chọn lớp trước, sau đó **Nhập CSV / Excel**. CSV đọc trực tiếp; `.xlsx` được đọc trong trình duyệt bằng SheetJS từ CDN (cần Internet ở lần đầu). Dùng hàng tiêu đề như sau:

```csv
studentCode,fullName,dateOfBirth,group
HS001,Nguyễn Văn A,2010-05-12,Nhóm 1
HS002,Trần Văn B,2010-08-20,Nhóm 1
```

Có thể dùng các tiêu đề tiếng Việt tương ứng: `Mã học sinh`, `Họ và tên`, `Ngày sinh`, `Nhóm`. Giao diện gọi API preview để kiểm tra dòng thiếu dữ liệu/mã trùng trước khi xác nhận. Sau xác nhận, mỗi học sinh hợp lệ được tạo trong một transaction; file Excel tải xuống có họ tên, mã học sinh, username và activation code. Bảo vệ file này như thông tin nhạy cảm và xóa sau khi chuyển mã an toàn. Chưa hỗ trợ `.xls`, `.doc/.docx` và import sự việc từ bảng tính.

Với chức năng **Dán danh sách**, giáo viên có thể dán riêng cột họ tên (mỗi dòng một học sinh), hoặc dán bảng có mã học sinh. Nếu bỏ trống mã, máy chủ tự sinh mã dạng `HS0001`, `HS0002`… dựa trên các mã hiện có và giữ mã không trùng; username được tạo từ mã (ví dụ `hs0002`). Sau khi xác nhận tạo, chọn **Tải danh sách Excel** để lưu họ tên, mã học sinh, username và activation code. File chứa mã kích hoạt chỉ được tạo một lần; hãy lưu riêng tư và chuyển đúng mã cho từng học sinh.

### 4.4 Kích hoạt và đăng nhập

Học sinh mở `/activate`, nhập activation code, student code và tự đặt mật khẩu. Mã chỉ dùng được một lần; account chuyển từ `PENDING_ACTIVATION` sang `ACTIVE`. Không có chức năng cho giáo viên xem mật khẩu. Mật khẩu quên cần một quy trình reset riêng; phiên bản hiện tại chưa cung cấp gửi email reset.

### 4.5 Chuyển năm học

Tạo năm mới và lớp đích, mở **Năm học & chuyển lớp**, chọn năm cũ/năm mới, lớp nguồn/đích, chọn học sinh rồi xác nhận. Server tạo membership mới với vai trò `STUDENT` mặc định. Không tạo account/profile mới, không copy điểm, và không sửa membership cũ. Giáo viên có thể chỉnh vai trò cán sự theo membership của năm phù hợp.

Trong mục **Năm học & lớp**, giáo viên chủ nhiệm có thể xóa lớp mình phụ trách sau khi nhập chính xác tên lớp để xác nhận. Thao tác này đưa lớp khỏi danh sách đang hoạt động và đóng các membership hiện tại; tài khoản, hồ sơ học sinh, điểm rèn luyện, bài đăng và dữ liệu lịch sử vẫn được giữ trong cơ sở dữ liệu. Nhật ký ghi nhận người thực hiện. Quản trị viên cần quyền `classes.manage`.

### 4.6 Điểm rèn luyện và tổ trưởng

Trong mục **Học sinh**, giáo viên chọn loại đơn vị quản lý của lớp là **Tổ** hoặc **Nhóm**. Lựa chọn áp dụng cho toàn lớp; mỗi đơn vị có tối đa một người đứng đầu. Với lớp dùng nhóm, giáo viên chọn chức vụ **Nhóm trưởng**; quyền xem thành viên, đề xuất điểm và đăng kết quả tuần tương tự tổ trưởng nhưng chỉ giới hạn trong nhóm được phân công. Tổ trưởng/nhóm trưởng chỉ được đề xuất ghi nhận theo quy tắc điểm giáo viên đã thiết lập, không thể tự nhập điểm hoặc ghi nhận học sinh ngoài đơn vị. Trên điện thoại, người đứng đầu có thể chạm giữ mức điểm rồi kéo thả vào thẻ học sinh; tổng điểm tuần đã được xác minh hiển thị bên cạnh tên. Có thể thay vào đó chọn mức điểm trong danh sách của học sinh và nhấn **Gửi**. Ghi nhận ở trạng thái `PENDING`, chỉ được tính vào tổng điểm sau khi giáo viên xác minh. Sau khi có điểm đã xác minh, tổ trưởng/nhóm trưởng có thể đăng bảng điểm tuần của đơn vị mình lên diễn đàn lớp; điểm đang chờ duyệt không được đưa vào bài đăng. Giáo viên cũng có thể tạo ghi nhận trực tiếp và đăng bảng thành tích toàn lớp.

### 4.7 Nhắc bài tập

Mục **Bài tập** chỉ dùng để giáo viên đăng tiêu đề, môn học, nội dung nhắc việc và hạn cần hoàn thành cho lớp. Học sinh xem thông báo trong dashboard hoặc mục Bài tập và thực hiện/nộp bài theo hướng dẫn của giáo viên bên ngoài ClassTrack. ClassTrack không thu bài và không chấm điểm bài tập; các API nộp bài/chấm bài cũ trả mã `410` và dữ liệu cũ (nếu có) không bị xóa.

### 4.8 Diễn đàn lớp

Thành viên có quyền truy cập lớp có thể đăng bài, bình luận và bày tỏ thích. Bảng tin sắp xếp bài mới nhất lên trước, hiển thị ảnh đại diện, người đăng, thời gian đăng và số lượt tương tác. Bài viết và bình luận được giới hạn ở lớp tương ứng phía server; nội dung người dùng được escape khi hiển thị. Diễn đàn hiện là văn bản, chưa có ảnh/video đính kèm, thông báo đẩy, báo cáo nội dung hoặc kiểm duyệt nâng cao.

### 4.9 Chỉnh ảnh đại diện

Mở mục **Hồ sơ cá nhân**, chọn ảnh PNG, JPEG hoặc WebP rồi bấm **Lưu hồ sơ**. Trình duyệt thu nhỏ ảnh xuống tối đa 512 × 512 và nén trước khi gửi; server giới hạn ảnh đã lưu tối đa 350 KB. Có thể đổi tên hiển thị hoặc xóa ảnh. Mỗi tài khoản chỉ sửa hồ sơ của chính mình. Ảnh mới xuất hiện ở bảng tin và cạnh bình luận sau khi tải lại nội dung.

## 5. Phân quyền và bảo vệ dữ liệu

- Role `ADMIN`, `TEACHER`, `STUDENT` được xác định ở server; người dùng không tự đăng ký rồi chọn quyền.
- Giáo viên chỉ truy cập lớp mình phụ trách; học sinh chỉ đọc dữ liệu của chính mình và lớp có membership.
- Server kiểm tra quyền trên API, không dựa riêng vào ẩn nút giao diện.
- Dùng cookie session `HttpOnly`, `SameSite=Strict`, token CSRF cho thao tác thay đổi dữ liệu; activation bị giới hạn số lần thử theo IP.
- Hạn chế body API 2 MB; truy vấn SQLite dùng parameter binding.
- Audit log lưu một số thao tác tạo account, tạo hồ sơ, kích hoạt, chuyển năm và thay đổi vai trò; không lưu password hay plaintext activation code.
- Tệp minh chứng chưa được upload/lưu; không có public file URL trong phiên bản này.

Máy chủ mặc định dùng HTTP localhost và cookie chưa đặt `Secure`, phù hợp chạy local. Khi đặt sau reverse proxy HTTPS, bật thuộc tính Secure cho session cookie trước khi khởi động backend:

```powershell
$env:CLASSTRACK_COOKIE_SECURE = "1"
python server.py
```

Chỉ bật tùy chọn này khi người dùng truy cập ứng dụng qua HTTPS; cookie Secure sẽ không được trình duyệt gửi qua HTTP. Ứng dụng bổ sung các header chống nhúng trang, hạn chế referrer và quyền truy cập thiết bị, nhưng không tự cung cấp TLS. Không dùng cấu hình này để xử lý dữ liệu học sinh thật trên Internet nếu chưa có HTTPS, rate limit đăng nhập phù hợp, quy trình backup/restore đã thử nghiệm, quản lý retention/xóa dữ liệu, chính sách phân quyền trường học và rà soát yêu cầu pháp lý.

## 6. API chính

Các endpoint yêu cầu đăng nhập và kiểm tra CSRF với POST/PATCH, trừ setup, login và kích hoạt:

| Method | Đường dẫn | Mục đích |
|---|---|---|
| GET | `/api/status`, `/api/me` | Trạng thái setup, tài khoản hiện tại |
| POST | `/api/me/profile` | Cập nhật tên hiển thị/ảnh đại diện của tài khoản đang đăng nhập |
| POST | `/api/login`, `/api/logout` | Đăng nhập/đăng xuất |
| CLI | `python server.py bootstrap-super-admin` | Tạo Super Admin đầu tiên; từ chối chạy lại sau khi đã có Super Admin |
| GET | `/api/admin/overview`, `/api/admin/administrators`, `/api/admin/audit-logs` | Dashboard và dữ liệu quản trị; endpoint nhạy cảm chỉ dành cho Super Admin |
| POST | `/api/admin/invitations` | Super Admin mời quản trị viên theo email và danh sách quyền |
| POST | `/api/admin/invitations/accept` | Người nhận đặt thông tin đăng nhập, chấp nhận lời mời dùng một lần |
| PATCH | `/api/admin/administrators/{id}` | Super Admin cấp quyền, khóa/mở khóa hoặc thu hồi quyền |
| POST | `/api/admin/invitations/{id}/revoke` | Super Admin hủy lời mời đang chờ |
| POST | `/api/teachers` | Admin tạo giáo viên và mật khẩu khởi tạo riêng |
| GET/POST | `/api/years` | Liệt kê/tạo năm học |
| GET/POST | `/api/classes` | Liệt kê/tạo lớp |
| PATCH | `/api/classes/{id}` | Sửa tên lớp hoặc giáo viên phụ trách; thao tác được ghi vào nhật ký |
| DELETE | `/api/classes/{id}` | Đưa lớp khỏi danh sách hoạt động, đóng membership và giữ nguyên dữ liệu lịch sử |
| DELETE | `/api/admin/classes/{id}` | Admin có quyền `classes.manage` xóa vĩnh viễn lớp sau khi xác nhận đúng tên; tạo bản sao lưu trước, xóa dữ liệu riêng của lớp và giữ hồ sơ/tài khoản học sinh |
| DELETE | `/api/years/{id}` | Admin có quyền `classes.manage` xóa vĩnh viễn năm học và các lớp bên trong sau khi xác nhận; tạo bản sao lưu trước, giữ hồ sơ/tài khoản học sinh |
| DELETE | `/api/admin/academic-years/{id}` | Đường dẫn tương thích cũ cho API xóa vĩnh viễn năm học |
| DELETE | `/api/memberships/{id}` | Đưa học sinh ra khỏi lớp, giữ tài khoản và hồ sơ lịch sử |
| GET/POST | `/api/classes/{id}/students` | Xem/thêm học sinh trong lớp |
| PATCH | `/api/students/{id}` | Sửa họ tên, ngày sinh và nhóm trong lớp đang chọn |
| POST | `/api/classes/{id}/students/import/preview` | Kiểm tra danh sách import |
| POST | `/api/imports/{id}/confirm` | Xác nhận import và tạo hồ sơ/tài khoản/membership |
| POST | `/api/imports/{id}/cancel` | Hủy lượt nhập đang ở bước xem trước |
| GET | `/api/accounts?classId={id}` | Trạng thái tài khoản học sinh của lớp |
| POST | `/api/students/{id}/activation-code` | Tạo mã kích hoạt mới |
| POST | `/api/activation/complete` | Kích hoạt account và đặt password |
| POST | `/api/promote` | Tạo membership cho năm mới |
| POST | `/api/classes/{id}/unit-type` | Chọn Tổ hoặc Nhóm làm đơn vị quản lý cho lớp |
| POST | `/api/memberships/{id}/role` | Thay đổi vai trò trong membership |
| GET/POST | `/api/conduct-records`, `/api/conduct-rules` | Ghi nhận rèn luyện, giáo viên tạo quy tắc điểm |
| GET/POST | `/api/group/conduct`, `/api/group/conduct-records` | Tổ trưởng/nhóm trưởng xem thành viên đơn vị, nhập điểm theo quy tắc lớp và gửi đề xuất chờ giáo viên xác minh |
| POST | `/api/group/conduct/publish` | Tổ trưởng đăng bảng điểm tuần đã xác minh của tổ mình dưới dạng thông báo diễn đàn lớp |
| GET | `/api/conduct/leaderboard` | Giáo viên xem bảng thành tích tính từ ghi nhận đã xác minh |
| POST | `/api/conduct/leaderboard/publish` | Đăng bảng thành tích thành thông báo trên diễn đàn lớp |
| POST | `/api/conduct-records/{id}/confirm` | Giáo viên xác minh ghi nhận |
| GET | `/api/student/dashboard` | Dữ liệu dashboard giới hạn cho học sinh hiện tại |
| GET | `/api/dashboard/stats` | Thống kê dashboard giáo viên theo phạm vi lớp/năm học được cấp |
| GET/POST | `/api/forum`, `/api/forum/posts` | Bảng tin lớp, bài viết và thông báo; dùng `type=ALL`, `POST` hoặc `ANNOUNCEMENT` để lọc |
| POST | `/api/forum/posts/{id}/comments`, `/reactions` | Bình luận/thích bài |
| GET/POST | `/api/assignments` | Xem/tạo thông báo nhắc bài tập; không nhận bài nộp |

Tạo giáo viên qua API sau khi admin đã đăng nhập:

```json
POST /api/teachers
{
  "username": "gv_toan",
  "displayName": "Giáo viên Toán",
  "password": "MatKhauKhoDai10KyTu"
}
```

Trong trình duyệt gọi các API có cookie tự động gửi kèm; với POST/PATCH, đọc `csrfToken` từ `/api/me` và gửi header `X-CSRF-Token`.

## 7. Kiểm thử

Chạy automated test bằng Python chuẩn:

```powershell
python -m unittest -v test_server
```

38 kiểm thử tự động bao gồm luồng học sinh/năm học, bootstrap một lần, vô hiệu hóa setup công khai, băm và dùng lời mời một lần, chặn admin thường/giáo viên vượt quyền, thu hồi session khi khóa tài khoản, migration có backup, quyền Super Admin sửa dữ liệu, bảng tin thông báo, hủy import, lọc dashboard/lớp theo năm học và kiểm tra backup/restore cùng security headers.

## 8. Tệp trong dự án

- `index.html`: giao diện đăng nhập, kích hoạt, dashboard, quản lý học sinh, import có thể hủy, sửa thông tin lớp/học sinh, rèn luyện, bài tập, bảng tin lớp và thông báo có avatar, chỉnh hồ sơ cá nhân, năm học và chuyển lớp.
- `server.py`: SQLite schema, xác thực, API, phân quyền server-side, audit log và migration bổ sung loại bài đăng bảng tin.
- `test_server.py`: 38 automated tests cho các luồng cốt lõi, quyền truy cập, sao lưu/khôi phục và lọc năm học.
- `README_VI.md`: tài liệu này.

Schema được khởi tạo bằng `CREATE TABLE IF NOT EXISTS`, không xóa database hiện có. Tuy nhiên bản prototype cũ lưu ở `localStorage`, không có migration tự động sang SQLite. Hãy xuất danh sách cũ ra CSV, sao lưu thủ công và thử nhập vào database mới trước khi bỏ dữ liệu local.

Migration quản trị đã được áp dụng cho database `classtrack.sqlite3` hiện có sau khi tạo bản sao `classtrack.sqlite3.pre-admin-migration.bak`. Đã đối chiếu và giữ nguyên 4 tài khoản cùng 2 hồ sơ học sinh. Khi ứng dụng gặp database cũ khác, server cũng tạo bản SQLite backup có dấu thời gian trước khi đổi schema. Bản sao lưu chứa dữ liệu cá nhân, cần giữ riêng và không gửi công khai.

Migration bảng tin bổ sung cột `forum_posts.post_type` bằng `ALTER TABLE`, giữ các bài đăng cũ với loại mặc định `POST`. Server tạo bản backup có dấu thời gian trước khi cập nhật database cũ; không xóa dữ liệu tài khoản, lớp, học sinh hoặc bài đăng.

## 9. Quản trị viên và email lời mời

Bootstrap tạo Super Admin trong database đang được chọn bởi `CLASSTRACK_DB` (mặc định là `classtrack.sqlite3`). Nếu có tài khoản cũ nhưng chưa có Super Admin, lệnh bootstrap vẫn tạo được Super Admin mới và giữ nguyên tài khoản/dữ liệu cũ. Admin cũ không tự động được nâng thành Super Admin; Super Admin mới có thể cấp quyền phù hợp trong trang **Quản trị hệ thống**.

Admin thường chỉ làm được các thao tác có quyền được cấp. Quyền khả dụng gồm `dashboard.read`, `users.read`, `users.manage`, `classes.read`, `classes.manage`, `conduct.read`, `conduct.manage`, `audit_logs.read` và `system.settings`. Mọi endpoint kiểm tra quyền ở server. Giáo viên vẫn bị giới hạn theo các lớp được giao.

Lời mời có hiệu lực 48 giờ, dùng một lần; database chỉ lưu SHA-256 hash của token. Nếu chưa cấu hình SMTP, Super Admin cần chuyển liên kết được hiển thị ngay sau khi tạo lời mời và gửi riêng cho người nhận. Liên kết không được ghi vào server log hoặc audit log. Đặt cấu hình trước khi chạy máy chủ:

```powershell
$env:CLASSTRACK_PUBLIC_URL = "https://classtrack.example.edu"
$env:CLASSTRACK_SMTP_HOST = "smtp.example.edu"
$env:CLASSTRACK_SMTP_PORT = "587"
$env:CLASSTRACK_SMTP_USER = "classtrack@example.edu"
$env:CLASSTRACK_SMTP_PASSWORD = "secret-from-a-secure-vault"
$env:CLASSTRACK_SMTP_FROM = "ClassTrack <classtrack@example.edu>"
python server.py
```

Port 465 dùng SSL trực tiếp; các port khác dùng STARTTLS. Khi không có cấu hình email đầy đủ hoặc gửi email gặp lỗi, giao diện báo trạng thái gửi thủ công và chỉ trả link đúng một lần. Không đưa mật khẩu SMTP vào source code hoặc commit vào Git. Bản hiện tại chưa có xác thực hai lớp; trước khi đưa lên Internet cần triển khai HTTPS, bảo vệ secret, sao lưu/khôi phục đã kiểm tra và rà soát bảo mật/pháp lý cho dữ liệu học sinh.

## 10. Phần chưa triển khai

Để mô tả phạm vi chính xác trong báo cáo, những chức năng sau chưa hoàn chỉnh: tải/lưu minh chứng riêng tư và quyền tải xuống, PDF/Excel report, dashboard biểu đồ, bài kiểm tra, kho tài liệu, diễn đàn có media/moderation, nhận xét theo kỳ, import `.xls`/Word và import conduct, promotion preview theo danh sách cá nhân, bộ lọc năm học toàn cục cho mọi API, account recovery/email, thông báo, AI, multi-school tenancy, sao lưu tự động, database server dành cho nhiều worker, quản lý trường/hệ thống admin UI và triển khai Internet bảo mật. Không nên trình bày các chức năng này như đã hoạt động trong phiên bản hiện tại.

### Xóa lớp/năm học và kiểm tra tạo mới

- Giáo viên có thể dùng **Đóng lớp** để ẩn lớp khỏi danh sách hoạt động và giữ lịch sử.
- Quản trị viên có quyền `classes.manage` có thể xóa vĩnh viễn lớp hoặc cả năm học. Giao diện yêu cầu nhập chính xác tên để xác nhận; API kiểm tra quyền ở backend.
- Trước khi xóa vĩnh viễn, hệ thống tạo một bản sao SQLite cạnh tệp cơ sở dữ liệu với hậu tố `.before-delete-...bak`. Tên bản sao được trả về sau thao tác để có thể tìm và phục hồi khi cần.
- Xóa vĩnh viễn loại bỏ dữ liệu thuộc lớp/năm học (membership, ghi nhận rèn luyện, quy tắc, bài nhắc, bài đăng và bài nộp liên quan). Hồ sơ học sinh và tài khoản lâu dài không bị xóa.
- Khi tạo năm học, hệ thống kiểm tra tên, ngày hợp lệ, thứ tự ngày và tên trùng. Nếu chọn `ACTIVE`, năm đang hoạt động trước đó tự chuyển sang `ARCHIVED` cùng transaction; thao tác này được ghi vào nhật ký. Khi tạo lớp, tên phải hợp lệ, năm học phải tồn tại và tên lớp không được trùng trong cùng năm.
