# deepseek-worker (`dsw`)

`dsw` cho phép **Claude Code đóng vai trò lập kế hoạch và review**, còn **DeepSeek làm worker giá rẻ**.

- Mỗi worker là một agent [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) chạy headless.
- Worker có vòng lặp tool riêng và làm việc trong git worktree riêng.
- Tool của worker chạy trong sandbox `bubblewrap`.
- `dsw` lo phần điều phối: chạy song song, theo dõi, kiểm tra kết quả bằng `--check`, tự nâng từ Flash lên Pro khi thất bại, đo chi phí, và có dashboard.

> **Trạng thái: thử nghiệm.** Phụ thuộc dsh bản RC (`0.2.2-rc.3`), API có thể đổi. Đọc mục [Mô hình bảo mật](#mô-hình-bảo-mật) trước khi dùng trên repo có dữ liệu thật.

```
Claude Code ── dsw run ──► dsw (worktree, env sạch, theo dõi, check, escalate, metrics)
   ▲                         │  dsh --profile headless --json
   │ dsw result / dashboard  ▼
   │                       dsh ──► DeepSeek API (Flash / Pro)
   │                         ├─ dsh-secret-guard: che secret, chặn tool file ra ngoài workspace,
   │                         │                    chặn web_fetch vào mạng nội bộ
   │                         └─ sandbox/dsw-bwrap: $HOME rỗng, không /run, không mạng
   │                                               (trừ proxy allowlist khi có --net)
   └──────── kết quả: diff, file đổi, output check, lời worker (chưa kiểm chứng)
```

Repo đi kèm: [dsh-secret-guard](https://github.com/devtdq1701/dsh-secret-guard), plugin của dsh lo phần che secret và giới hạn tool file. `dsw setup` sẽ cài plugin này vào dsh.

## Mục lục

1. [Yêu cầu hệ thống](#yêu-cầu-hệ-thống)
2. [Cài đặt](#cài-đặt)
3. [Dùng với Claude Code](#dùng-với-claude-code)
4. [Các lệnh](#các-lệnh)
5. [Cấu hình](#cấu-hình)
6. [Mô hình bảo mật](#mô-hình-bảo-mật)
7. [Chi phí](#chi-phí)
8. [Xử lý sự cố](#xử-lý-sự-cố)
9. [Phát triển](#phát-triển)

## Yêu cầu hệ thống

| Thành phần | Phiên bản | Ghi chú |
|---|---|---|
| Linux x86_64 | — | Đã kiểm trên Ubuntu 24.04. macOS chưa hỗ trợ vì sandbox dùng `bwrap` |
| Python | ≥ 3.11 | Đọc `config.toml` bằng `tomllib`; chỉ dùng thư viện chuẩn |
| Node.js | ≥ 24.9 | dsh yêu cầu |
| pnpm | qua `corepack` | `dsh plugin add` dùng pnpm |
| `bubblewrap` (`bwrap`), `socat`, `iproute2` (`ip`), `git`, `curl` | — | Sandbox, cầu nối proxy, kiểm tra route |
| Claude Code | — | Để Claude điều phối; `dsw` cũng chạy độc lập được |
| DeepSeek API key | — | Lấy tại [platform.deepseek.com](https://platform.deepseek.com) |

## Cài đặt

Các bước dưới đây giả định dùng `bash`/`zsh` trên Ubuntu/Debian, và clone mã nguồn vào `~/src`. Đổi đường dẫn tuỳ ý.

### Bước 1. Gói hệ thống

```bash
sudo apt update && sudo apt install -y bubblewrap socat iproute2 git curl python3
```

Fedora: `sudo dnf install -y bubblewrap socat iproute git curl python3`.

Kiểm tra: `bwrap --version` in ra phiên bản (ví dụ `bubblewrap 0.9.0`).

### Bước 2. Node.js 24 và pnpm

Nếu chưa có Node ≥ 24.9, cài qua [nvm](https://github.com/nvm-sh/nvm):

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
exec "$SHELL"            # nạp nvm vào shell hiện tại
nvm install 24
corepack enable pnpm
node --version           # >= v24.9
```

`dsw` tự tìm Node ≥ 24.9 trên `PATH`, trong nvm, fnm hoặc volta. Node mặc định của máy không cần là 24.

### Bước 3. Cài dsh vào một runtime riêng

`dsw` tự tìm dsh ở `~/.local/share/dsw/dsh-runtime`. Cài vào đó:

```bash
mkdir -p ~/.local/share/dsw/dsh-runtime && cd ~/.local/share/dsw/dsh-runtime
cat > package.json <<'EOF'
{
  "name": "dsh-runtime",
  "private": true,
  "allowScripts": {
    "@deepseek-ai/dsh-subprocess-local": true,
    "koffi": true,
    "node-pty": true,
    "protobufjs": true
  }
}
EOF
printf 'legacy-peer-deps=false\n' > .npmrc
npm install @deepseek-ai/dsh@0.2.2-rc.3
./node_modules/.bin/dsh --version     # 0.2.2-rc.3
```

Lưu ý khi cài:
- **`.npmrc` với `legacy-peer-deps=false` là bắt buộc.** Các gói `@deepseek-ai/*` khai báo nhau là peer dependency. Nếu `~/.npmrc` của máy đặt `legacy-peer-deps=true`, npm sẽ không cài các gói đó và dsh báo `ERR_MODULE_NOT_FOUND`. File `.npmrc` cục bộ này ghi đè cấu hình chung chỉ trong thư mục runtime.
- **`allowScripts`** cho phép build các module native mà dsh cần (npm 11 chặn install script theo mặc định).
- **Không cài dsh global (`npm install -g`).** Các bản RC hiện tại thiếu dependency khi cài theo kiểu global.

### Bước 4. Lấy mã nguồn

Clone hai repo vào **cùng một thư mục cha**, để `dsw` tự tìm thấy plugin:

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/devtdq1701/deepseek-worker.git
git clone https://github.com/devtdq1701/dsh-secret-guard.git
mkdir -p ~/.local/bin && ln -sf ~/src/deepseek-worker/dsw.py ~/.local/bin/dsw
dsw --help | head -3
```

Nếu `~/.local/bin` chưa có trong `PATH`, thêm dòng `export PATH="$HOME/.local/bin:$PATH"` vào `~/.bashrc` hoặc `~/.zshrc`.

### Bước 5. Lưu DeepSeek API key

Key nằm trong một file chỉ chủ sở hữu đọc được. Lệnh dưới đây dùng `read -s` nên key không hiện trên màn hình và không vào lịch sử shell:

```bash
mkdir -p ~/.config/dsw
( umask 077; printf 'DeepSeek API key: '; read -rs k; echo; printf '%s' "$k" > ~/.config/dsw/api_key; unset k )
ls -l ~/.config/dsw/api_key      # -rw-------
```

Cách khác: đặt biến môi trường `DEEPSEEK_API_KEY`. `dsw` ưu tiên đọc file trước.

### Bước 6. Cấu hình dsh bằng `dsw setup`

```bash
dsw setup --write-config
```

Lệnh này làm 4 việc:
1. Tạo `~/.config/dsw/config.toml` từ những gì tự dò được: đường dẫn dsh, Node, plugin.
2. Cài `dsh-secret-guard` vào profile `headless` của dsh (`dsh plugin --profile headless add link:...`).
3. Ghi một khối `# >>> dsw managed` vào `~/.dsh/profiles/headless/cordis.patch.yml`. Khối này gồm:
   - mount plugin cùng cấu hình (danh sách đường dẫn cần che, danh sách toolchain được đọc);
   - cho `agent-loop` phụ thuộc vào service `secretGuard`, để thiếu plugin thì dsh **không khởi động được**;
   - trỏ `sandbox.runnerCommand` tới `sandbox/dsw-bwrap`.
4. Chạy `dsh --dump-config` để xác minh, và in mỗi mục một dòng `✓`/`✗`.

Lệnh chạy lại bao nhiêu lần cũng được, và không đụng tới các entry anh tự thêm vào patch. Dùng `--dry-run` để xem trước thay đổi.

### Bước 7. Kiểm tra bằng `dsw doctor`

```bash
dsw doctor
```

Kết quả đạt: mọi dòng đều bắt đầu bằng `✓`. Lệnh này cũng in ra từng giá trị cấu hình kèm nguồn gốc của nó (`config`, `discovered`, `default`, `env`).

### Bước 8. Chạy thử

```bash
mkdir -p /tmp/dsw-demo && cd /tmp/dsw-demo && git init -q && git commit -q --allow-empty -m init
dsw run --repo . --worktree \
  --task "Tạo hello.py in ra 'xin chào'. Không commit." \
  --check "python3 hello.py | grep -q 'xin chào'"
```

Kết quả mong đợi:
- Exit code `0`.
- Output có dòng `check ... -> PASS`.
- Chi phí khoảng $0.002–0.005.

Code worker viết nằm ở `.worktrees/dsw-<id>`, trên branch `dsw/<id>`.

## Dùng với Claude Code

Gắn skill và 2 agent của repo vào Claude Code:

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
ln -sfn ~/src/deepseek-worker/claude/skills/deepseek-worker ~/.claude/skills/deepseek-worker
ln -sf ~/src/deepseek-worker/claude/agents/dsw-flash.md ~/.claude/agents/dsw-flash.md
ln -sf ~/src/deepseek-worker/claude/agents/dsw-pro.md ~/.claude/agents/dsw-pro.md
```

Mở phiên Claude Code mới để nạp. Sau đó:
- Bảo Claude kiểu "giao phần này cho DeepSeek làm". Skill `deepseek-worker` hướng dẫn Claude:
  - viết đề bài tự đủ cho worker;
  - chạy `dsw run --worktree --check ...` ở chế độ nền;
  - review bằng `dsw result`;
  - tự chạy lại test trước khi báo xong.
- Agent `dsw-flash` / `dsw-pro` dùng Haiku làm lớp chuyển tiếp, giúp job hiện trong task panel như một subagent. Giới hạn mỗi job 540 giây vì Bash của Claude Code bị cắt ở 10 phút; job dài hơn thì chạy `dsw run` trực tiếp ở chế độ nền.

## Các lệnh

| Lệnh | Việc |
|---|---|
| `dsw run --repo R [--worktree] --task "…"\|--task-file F` | Chạy một worker. Tuỳ chọn thêm: `--check "<lệnh>"`, `--tier flash\|pro`, `--effort off\|low\|high\|max`, `--escalate`, `--max-steps N`, `--timeout S`, `--net [allowlist\|public]`, `--allow-host H`, `--id ID` |
| `dsw resume <id> --task "…"` | Giao tiếp việc cho worker đó, trên cùng session |
| `dsw status [id] [--all] [--json]` | Bảng job: trạng thái, tier, số bước, số tool, token (tách cache), chi phí, tool đang chạy |
| `dsw tail <id> [-n N]` | Các lệnh và kết quả gần nhất của worker |
| `dsw result <id> [--json]` | Kết quả theo kiểu "sự thật trước": diff, file đổi, output check, các attempt, cuối cùng mới là lời worker tự báo (chưa kiểm chứng) |
| `dsw cancel <id>` | Dừng job và kill cả cây process |
| `dsw clean <id> [--force]` | Xoá worktree, giữ branch. Từ chối nếu còn thay đổi chưa commit hoặc nếu con trỏ `.git` đã bị tráo |
| `dsw stats [--days N]` | Chi phí, tỉ lệ pass check, cache hit theo tier, số job chạy giờ cao điểm, số dư tài khoản |
| `dsw serve [--port P]` | Dashboard chỉ đọc tại `http://127.0.0.1:8791` |
| `dsw statusline` | Một dòng tóm tắt để gắn vào statusline |
| `dsw preflight` / `dsw doctor` / `dsw setup` | Kiểm tra nhanh / chẩn đoán đầy đủ / cấu hình dsh |

**Exit code của `run` và `resume`:**

| Code | Ý nghĩa |
|---|---|
| 0 | xong (check pass, nếu có check) |
| 1 | bị từ chối trước khi chạy |
| 2 | thất bại, bị huỷ, quá thời gian, lặp, hoặc quá số bước |
| 3 | check thất bại |
| 4 | worker cần hỏi lại (kết thúc bằng câu hỏi mà không sửa file nào) |

**Mạng cho tool của worker:**

| Cờ | Tool ra mạng được tới đâu |
|---|---|
| (không có) | Không có mạng. `web_search` và `web_fetch` của dsh vẫn dùng được để tra cứu |
| `--net` | Chỉ HTTPS tới allowlist: pypi, npm/yarn, github, go proxy, crates, maven, cộng `--allow-host` |
| `--net public` | HTTPS tới mọi host public |

Cả ba mức đều luôn chặn IP private, mọi địa chỉ thuộc route cụ thể (VPN, cluster, mạng văn phòng), và kết nối không phải HTTPS. Mọi lần cho phép hay chặn đều ghi vào `~/.local/state/dsw/jobs/<id>/proxy.log`.

## Cấu hình

Thứ tự ưu tiên: **cờ CLI > biến `DSW_*` > `~/.config/dsw/config.toml` > tự dò > mặc định**. Mọi khoá đều không bắt buộc.

```toml
dsh = "~/.local/share/dsw/dsh-runtime/node_modules/.bin/dsh"
node_bin = "~/.nvm/versions/node/v24.11.0/bin"
key_file = "~/.config/dsw/api_key"
profiles = ["headless"]          # thêm "web" nếu dùng hub dsh web (ví dụ với dsh-crew)
secret_guard = "~/src/dsh-secret-guard"
# gate = "~/my-hooks/gate.py"    # gate riêng theo chuẩn hook Claude Code; bỏ trống = gate đi kèm plugin
serve_port = 8791

[defaults]
tier = "flash"
effort_flash = "high"
effort_pro = "high"
max_steps = 60
timeout = 1800

[net]
allow = ["gitlab.com"]           # host thêm cho --net

[sandbox]
hide = ["~/secret-dir"]          # đường dẫn che thêm
home_allow = ["~/.sdkman"]       # toolchain cho worker đọc (chỉ đọc)
```

| Khoá | Biến môi trường | Tự dò / mặc định |
|---|---|---|
| `dsh` | `DSW_DSH` | `dsh` trên `PATH`, sau đó `~/.local/share/dsw/dsh-runtime` |
| `node_bin` | `DSW_NODE_BIN` | Node ≥ 24.9 mới nhất trên `PATH`, nvm, fnm, volta |
| `key_file` | `DSW_KEY_FILE` | `~/.config/dsw/api_key`; nếu không có thì dùng `DEEPSEEK_API_KEY` |
| `state_dir` | `DSW_HOME` | `${XDG_STATE_HOME:-~/.local/state}/dsw` |
| `dsh_home` | `DSH_HOME` | `~/.dsh` |
| `secret_guard` | `DSW_SECRET_GUARD` | `plugin/` hoặc `../dsh-secret-guard` cạnh `dsw.py` |
| `gate` | `DSW_GATE` | trống, tức là dùng `gate/secret_gate.py` của plugin |
| `serve_port` | `DSW_SERVE_PORT` | 8791 |

Các file danh sách:
- `sandbox/hidden-paths.txt`: các đường dẫn credential luôn bị che.
- `sandbox/home-allow.txt`: các toolchain mà worker được đọc.

Mỗi file có bản bổ sung cho từng máy trong `~/.config/dsw/` (`*.local.txt`). Sau khi đổi cấu hình, chạy lại `dsw setup`.

## Mô hình bảo mật

Worker được coi là **có thể bị thao túng**: model, hoặc nội dung trong repo và trang web nó đọc, có thể chèn chỉ thị. Các lớp bảo vệ:

| Lớp | Bảo vệ gì |
|---|---|
| **`$HOME` rỗng** | Tool bash thấy `$HOME` là tmpfs trống. Chỉ có workspace, các toolchain trong `home-allow.txt` (chỉ đọc), và thư mục `.git` gốc của worktree (chỉ đọc, chỉ mount khi đối chiếu ngược hợp lệ) |
| **Không `/run`, không mạng** | Không với tới D-Bus, systemd, `docker.sock`, ssh/gpg agent, hay socket proxy của job khác. Có `--new-session`, `--unshare-ipc`, `--unshare-net` |
| **Tool file bị giới hạn** (plugin) | `read`/`write`/`edit`/`grep`/`glob`/`apply_patch`/`working_directory` chạy ngoài bwrap, nên plugin giới hạn chúng: resolve realpath theo cwd của session; không đọc được `$HOME` ngoài workspace hay toolchain; không ghi ra ngoài workspace; cấm `..`, `~user`, `/proc`, `/sys`, `/dev` |
| **Che secret** (plugin + gate) | Thay secret trong output tool bằng `<REDACTED:...>`. File nào từng bị che thì các lệnh sau nhắc tới file đó bị chặn (taint) |
| **`web_fetch`** | Chặn tên nội bộ, IP private, các dạng IPv6 nhúng IPv4, và địa chỉ thuộc route VPN |
| **Check và git chạy trong sandbox** | `--check`, `git status`/`diff` để lấy kết quả, và `dsw clean` đều chạy trong sandbox, nên test, hook hay cấu hình git do worker cài không chạy được trên máy |
| **Env sạch** | Worker và lệnh check chỉ nhận các biến trong allowlist |
| **Fail-closed** | Thiếu plugin hoặc self-test thất bại thì dsh không khởi động. Gate lỗi thì giữ output lại. Proxy giới hạn 32 kết nối đồng thời |

Các lớp này đã qua một vòng review độc lập ngày 2026-11-16. Các lỗ tìm được (thoát sandbox qua socket của máy, check chạy trên máy, git config độc, lách đường dẫn, SSRF qua IPv6, DNS rebinding vào dashboard…) đều đã vá, có test hồi quy trong `tests/test_security.py`.

**Rủi ro còn lại:**
- Che secret theo pattern chỉ là best effort. Secret định dạng lạ, hoặc bị copy rồi biến đổi trong workspace, sẽ không bị che. **Không để credential thật trong workspace giao cho worker.**
- Với `--net`, các host trong allowlist (như github) vẫn có thể bị dùng để đẩy dữ liệu ra ngoài. `--net public` thì tới được mọi host public.
- **Code và dữ liệu trong workspace được gửi tới DeepSeek** (server đặt tại Trung Quốc, theo chính sách quyền riêng tư của DeepSeek).
- `web_fetch` vẫn có thể bị DNS rebinding giữa lúc kiểm tra và lúc fetch.
- Worker đọc được `.git/config` của repo, nên nếu URL remote có chứa token thì token đó lộ.

## Chi phí

Bảng giá DeepSeek tính theo USD cho mỗi 1 triệu token, tại giờ cao điểm. Ngoài giờ cao điểm giá bằng một nửa.

| Model | Cache hit | Cache miss | Output |
|---|---|---|---|
| Flash | 0.006 | 0.30 | 1.20 |
| Pro | 0.044 | 1.32 | 3.96 |

- Giờ cao điểm: 01–04h và 06–10h UTC, thứ Hai đến thứ Sáu.
- Đo thực tế: một task code nhỏ chạy Flash tốn khoảng $0.003 và 15–30 giây; cache hit khoảng 90%.
- `dsw stats` báo chi phí thực tế của từng loại job.

## Xử lý sự cố

| Triệu chứng | Nguyên nhân và cách xử lý |
|---|---|
| dsh báo `ERR_MODULE_NOT_FOUND` | Thiếu peer dependency do `legacy-peer-deps=true`. Tạo `.npmrc` trong runtime với `legacy-peer-deps=false` (Bước 3) |
| `bwrap: setting up uid map: Permission denied` | AppArmor chặn user namespace (Ubuntu ≥ 23.10). Dùng gói `bubblewrap` của distro, vì nó đi kèm profile AppArmor cho phép; không dùng bản tự build |
| `dsw run` báo `refused: ... dsw-bwrap not configured` | Chưa chạy `dsw setup`, hoặc vừa nâng dsh. Chạy `dsw setup` rồi `dsw doctor` |
| `dsw run` báo `no DeepSeek key` | Thiếu `~/.config/dsw/api_key` (Bước 5) hoặc biến `DEEPSEEK_API_KEY` |
| Worker không cài được package | Thiếu `--net`. Hoặc host không có trong allowlist: xem `proxy.log`, rồi thêm `--allow-host` |
| `git` trong worktree báo `not a git repository` | Repo gốc không còn ở vị trí cũ, hoặc con trỏ `.git` đã bị sửa. Tạo lại worktree |
| Cổng 8791 đã bị dùng | Đặt `serve_port` khác trong `config.toml` |
| Chạy trong Claude Desktop thì `claude -p` báo "Not logged in" | Không liên quan tới `dsw`. Shell con của Desktop mang env và auth của phiên Desktop |

## Phát triển

```bash
cd ~/src/deepseek-worker
python3 -m unittest tests/test_security.py tests/test_dsw.py tests/test_net.py   # "Ran 25 tests" … "OK"
```

- Test chạy offline với `tests/fake_dsh.py` (phát JSONL dựng sẵn), không gọi API. Test bảo mật cần `bwrap`.
- Plugin: `cd ~/src/dsh-secret-guard && npm test`.

| File | Vai trò |
|---|---|
| `dsw.py` | CLI: job, worktree, theo dõi, check, escalate, metrics |
| `dsw_config.py`, `dsw_setup.py` | Cấu hình; `setup` / `doctor` |
| `dsw_net.py` | Proxy egress cho từng job (`--net`) |
| `dsw_web.py` | Dashboard |
| `sandbox/dsw-bwrap`, `sandbox/*.txt` | Sandbox runner và các danh sách che / cho đọc |
| `claude/` | Skill và agent cho Claude Code |

**Nguồn gốc cơ chế:**
- Worktree, tổng hợp usage và quy ước exit code lấy từ mô hình review-loop với agy.
- Job board, tier/escalate, khoá workspace, chặn lồng worker và phát hiện job mồ côi lấy cảm hứng từ [dsh-crew](https://github.com/ZSeven-W/dsh-crew).

## Giấy phép

MIT. Xem [LICENSE](LICENSE).
