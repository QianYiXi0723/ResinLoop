using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System.Text.RegularExpressions;

namespace ResinLoop
{
    /// <summary>
    /// ResinLoop 桌面壳：静默托管 engine/ 便携 Python 引擎，WebView2 加载本地控制台。
    ///
    /// 生命周期契约：
    ///   - 双击/X 按钮       → 窗口藏进托盘，引擎与守护进程照常运行（壳进程活着 = Job 活着）
    ///   - 托盘「彻底退出」  → 收尸引擎（Job close 连带 daemon），不留任何幽灵进程
    ///   - 壳被强杀/崩溃     → Windows Job Object 内核级收尸，引擎同样活不过壳
    ///   - 引擎已在跑        → 直接复用，绝不重复拉起（Job 只收本壳拉起的进程）
    /// </summary>
    public class MainForm : Form
    {
        private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
        private static readonly string EnginePython = Path.Combine(Root, "engine", "python", "pythonw.exe");
        private static readonly string EngineApp = Path.Combine(Root, "engine", "app.py");
        private static readonly string ShellLog = Path.Combine(Root, "logs", "shell.log");

        //: 虚拟主机名：页面与静态资源由 WebView2 直接从 engine\ui 读盘，
        //: 不经过 Python 的本地 HTTP 服务。/api/* 由本进程代转到引擎端口。
        private const string UiHost = "resinloop.local";

        private readonly bool _startMinimized;
        private readonly int? _forcedPort;   // --port N：优先在该端口复用/拉起引擎（默认 8765→8899 探测）
        private Process _engine;          // 本壳拉起的引擎；复用现有实例时为 null（退出时不收尸）
        private IntPtr _job = IntPtr.Zero; // 引擎（连带它派生的 daemon）都挂在这个 Job 上
        private string _url = "http://127.0.0.1:8765/";
        private int _port = 8765;          // 本轮实际使用的控制台端口（托盘「立即巡检」用）
        private NotifyIcon _tray;
        private WebView2 _web;
        private bool _reallyExit;
        private bool _trayHintShown;
        private int _apiHits;             // 已被拦截代转的 /api/* 次数（只用于首条日志）

        public MainForm(bool startMinimized, int? forcedPort)
        {
            _startMinimized = startMinimized;
            _forcedPort = forcedPort;

            Text = "ResinLoop · 日常巡查";
            // 尺寸口径必须与脚本版 app.py 一致：980×800（逻辑像素），最小 820×620。
            // 980 落在页面 1000px 断点下方，走单列紧凑布局（右侧「凭证健康／设备伪装」
            // 栏折到页尾，往下滚即可见）。两个入口尺寸不一致时，同一份 UI 会呈现成
            // 两套信息架构。
            // PerMonitorV2 下 WinForms 的 Size 单位是物理像素，必须手动乘 DPI 系数
            // （与 pywebview 的 Size(w*scale, h*scale) 同口径）。不乘的话 125% 缩放时
            // 窗口只有 784 逻辑宽，比脚本版整整窄 20%。
            AutoScaleMode = AutoScaleMode.None;
            float dpiScale;
            using (Graphics screenG = Graphics.FromHwnd(IntPtr.Zero))
                dpiScale = screenG.DpiX > 0 ? screenG.DpiX / 96f : 1f;
            const int baseW = 980, baseH = 800, baseMinW = 820, baseMinH = 620;
            int wantW = (int)Math.Round(baseW * dpiScale);
            int wantH = (int)Math.Round(baseH * dpiScale);
            int minW = (int)Math.Round(baseMinW * dpiScale);
            int minH = (int)Math.Round(baseMinH * dpiScale);
            Rectangle wa = Screen.PrimaryScreen != null
                ? Screen.PrimaryScreen.WorkingArea
                : new Rectangle(0, 0, wantW, wantH);
            Size = new Size(Math.Max(minW, Math.Min(wantW, wa.Width - 40)),
                            Math.Max(minH, Math.Min(wantH, wa.Height - 40)));
            MinimumSize = new Size(minW, minH);
            StartPosition = FormStartPosition.CenterScreen;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { /* 图标缺省兜底 */ }

            bool dark = IsWindowsDark();
            BackColor = dark ? Color.FromArgb(26, 26, 26) : Color.FromArgb(244, 246, 249);

            _web = new WebView2
            {
                Dock = DockStyle.Fill,
                DefaultBackgroundColor = BackColor,   // 防首帧白闪
            };
            Controls.Add(_web);
            _web.CoreWebView2InitializationCompleted += OnWebInitDone;

            _tray = new NotifyIcon
            {
                Icon = Icon,
                Text = "ResinLoop · 日常巡查",
                Visible = true,
            };
            var menu = new ContextMenuStrip();
            menu.Items.Add("打开控制台", null, (s, e) => ShowMain());
            menu.Items.Add("立即巡检", null, (s, e) => RunManualCheck());
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("彻底退出", null, (s, e) => ReallyExit());
            _tray.ContextMenuStrip = menu;
            _tray.DoubleClick += (s, e) => ShowMain();

            Load += async (s, e) => await BootAsync();
            FormClosing += OnFormClosing;
        }

        // ------------------------------------------------------------ 启动

        private async Task BootAsync()
        {
            try
            {
                if (_startMinimized) Hide(); else Show();
                Log("壳启动 pid=" + Process.GetCurrentProcess().Id + (_startMinimized ? "（--tray 自启）" : ""));

                int port = PickPortWithOverride();
                if (port < 0)
                {
                    MessageBox.Show("8765 / 8899 / 8900-8909 全部不可用，无法启动。");
                    _reallyExit = true;
                    Close();
                    return;
                }
                _url = "http://127.0.0.1:" + port + "/";
                _port = port;

                if (IsResinLoopAlive(port))
                {
                    _engine = null;
                    Log("端口 " + port + " 检测到 ResinLoop 实例 → 直接复用，不重复拉起");
                }
                else
                {
                    Log("端口 " + port + " 无 ResinLoop 实例 → 静默拉起引擎（pythonw，无黑框）");
                    StartEngine(port);
                    bool ready = await Task.Run(() => WaitEngineReady(port, 20000));
                    if (!ready)
                    {
                        MessageBox.Show("引擎 20 秒内未就绪，详见 logs\\engine.log。");
                        _reallyExit = true;
                        Close();
                        return;
                    }
                    Log("引擎就绪 pid=" + (_engine == null ? "?" : _engine.Id.ToString()) + "，端口 " + port);
                }

                // 冷启动兜底：daemon 唯一入口是页面按钮（发行版无 start.bat）；fire-and-forget 防 6 秒白屏。
                _ = Task.Run(() => EnsureDaemon(port));

                Directory.CreateDirectory(Path.Combine(Root, "logs", "webview2"));
                var env = await CoreWebView2Environment.CreateAsync(
                    userDataFolder: Path.Combine(Root, "logs", "webview2"));
                await _web.EnsureCoreWebView2Async(env);

                // UI 走虚拟主机映射：index.html / app.js / style.css 由 WebView2 直接读 engine\ui，
                // 页面不再依赖 Python 的本地 HTTP 服务——引擎还没起完也能先把界面画出来，
                // 不占端口、不受系统代理影响，顺手把 /favicon.ico 的 404 也免了。
                // 前端 fetch('/api/...') 与图标 /static/... 由本进程拦截代转，JS 一行不改。
                if (TryMapUiFolder())
                {
                    _web.CoreWebView2.Navigate("https://" + UiHost + "/index.html");
                    Log("壳加载 https://" + UiHost + "/index.html（engine\\ui 页面接管，不走本地 HTTP）");
                }
                else
                {
                    _web.CoreWebView2.Navigate(_url);
                    Log("壳加载 " + _url + "（页面接管不可用，回退本地 HTTP）");
                }
            }
            catch (Exception ex)
            {
                Log("启动异常：" + ex);
                MessageBox.Show("启动失败：" + ex.Message);
            }
        }

        /// <summary>
        /// 挂上 https://resinloop.local/* 的拦截器：页面、样式、脚本、图标、接口全部由本进程应答。
        /// 不使用 SetVirtualHostNameToFolderMapping —— 实测映射到文件夹的主机不会触发
        /// WebResourceRequested，/api/* 就拦不住（页面会拿到 404 HTML，表现为 Failed to fetch）。
        /// 这里按官方静态服务样例的路线：不注册虚拟主机，整站自己兜。
        /// 失败（目录缺失、SDK 不支持）时返回 false，调用方回退到本地 HTTP。
        /// </summary>
        private bool TryMapUiFolder()
        {
            string uiDir = Path.Combine(Root, "engine", "ui");
            if (!Directory.Exists(uiDir))
            {
                Log("engine\\ui 不存在，跳过页面接管");
                return false;
            }
            try
            {
                _web.CoreWebView2.AddWebResourceRequestedFilter(
                    "https://" + UiHost + "/*", CoreWebView2WebResourceContext.All);
                _web.CoreWebView2.WebResourceRequested += OnWebResourceRequested;
                _web.CoreWebView2.NavigationCompleted += OnPageLoaded;
                _web.CoreWebView2.WebMessageReceived += OnWebMessage;
                return true;
            }
            catch (Exception ex)
            {
                Log("页面接管失败：" + ex.Message);
                return false;
            }
        }

        /// <summary>页面加载完成后做一次 DOM 自检，结果写进 logs\shell.log，供构建门禁断言。</summary>
        private async void OnPageLoaded(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            Log("页面加载 " + (e.IsSuccess ? "成功" : "失败：" + e.WebErrorStatus));
            if (!e.IsSuccess) return;
            try
            {
                string n = await _web.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('.tabpanel').length");
                Log("DOM 自检 tabpanel=" + n);
                // 探针：在页面里真发一次 fetch('/api/env')，验证 /api/* 确实被壳拦截代转，
                // 而不是打到了虚拟主机的静态目录（那样会拿到 404 HTML）。
                // 注意 ExecuteScriptAsync 不等待 Promise（只会返回 {}），所以结果用 web message 回传。
                await _web.CoreWebView2.ExecuteScriptAsync(
                    "fetch('/api/env').then(r => r.text().then(t =>" +
                    " window.chrome.webview.postMessage('rlprobe:' + r.status + '/LEN' + t.length)))" +
                    ".catch(e => window.chrome.webview.postMessage('rlprobe:ERR:' + e));");
            }
            catch (Exception ex)
            {
                Log("DOM 自检异常：" + ex.Message);
            }
        }

        private void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string msg;
            try { msg = e.TryGetWebMessageAsString(); }
            catch { return; }
            if (msg == null) return;
            if (msg.StartsWith("rlprobe:", StringComparison.Ordinal))
                Log("API 探针 " + msg.Substring("rlprobe:".Length));
        }

        private void OnWebResourceRequested(object sender, CoreWebView2WebResourceRequestedEventArgs e)
        {
            string uri = e.Request.Uri;
            int at = uri.IndexOf("/api/", StringComparison.OrdinalIgnoreCase);
            if (at >= 0)
            {
                if (Interlocked.Increment(ref _apiHits) == 1)
                    Log("首个 API 拦截：" + uri.Substring(at));
                ForwardToEngine(uri.Substring(at), e.Request.Method, e);
                return;
            }
            at = uri.IndexOf("/static/", StringComparison.OrdinalIgnoreCase);
            if (at >= 0)
            {
                ServeLocalStatic(uri.Substring(at), e);
                return;
            }
            ServeUiAsset(uri, e);
        }

        /// <summary>页面本体：index.html / app.js / style.css 等，全部从 engine\ui 读盘。</summary>
        private void ServeUiAsset(string uri, CoreWebView2WebResourceRequestedEventArgs e)
        {
            string path;
            try { path = new Uri(uri).AbsolutePath; }
            catch { path = "/index.html"; }
            if (string.IsNullOrEmpty(path) || path == "/") path = "/index.html";

            // 浏览器总会自发请求 /favicon.ico；这里给 204，免得控制台里挂一条 404 噪音
            if (string.Equals(path, "/favicon.ico", StringComparison.OrdinalIgnoreCase))
            {
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(new byte[0]), 204, "No Content", "Cache-Control: no-store\r\n");
                return;
            }

            string baseDir = Path.GetFullPath(Path.Combine(Root, "engine", "ui"));
            string full = Path.GetFullPath(Path.Combine(baseDir, path.TrimStart('/').Replace('/', Path.DirectorySeparatorChar)));
            if (!full.StartsWith(baseDir, StringComparison.OrdinalIgnoreCase) || !File.Exists(full))
            {
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(Encoding.UTF8.GetBytes("not found")), 404, "Not Found",
                    "Content-Type: text/plain; charset=utf-8\r\n");
                return;
            }
            try
            {
                byte[] body;
                string ctype = GuessContentType(Path.GetExtension(full));

                // index.html 是模板：__STYLE__ / __APP_JS__ 两个占位符要内联成 style.css 与 app.js，
                // 与 login_ui.py 的 _assemble_page() 保持同一口径（那边是 HTTP 路径在用）。
                // 不做这一步，页面会静默丢掉全部 JS（表现为所有 tab 都停在「读取中…」）。
                if (string.Equals(Path.GetFileName(full), "index.html", StringComparison.OrdinalIgnoreCase))
                {
                    string html = File.ReadAllText(full, Encoding.UTF8);
                    string cssPath = Path.Combine(baseDir, "style.css");
                    string jsPath = Path.Combine(baseDir, "app.js");
                    if (!File.Exists(cssPath) || !File.Exists(jsPath))
                        throw new FileNotFoundException("ui\\style.css 或 ui\\app.js 缺失");
                    html = html.Replace("__STYLE__", File.ReadAllText(cssPath, Encoding.UTF8))
                               .Replace("__APP_JS__", File.ReadAllText(jsPath, Encoding.UTF8));
                    body = Encoding.UTF8.GetBytes(html);
                    ctype = "text/html; charset=utf-8";
                }
                else
                {
                    body = File.ReadAllBytes(full);
                }

                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(body), 200, "OK",
                    "Content-Type: " + ctype + "\r\nCache-Control: no-store\r\n");
            }
            catch (Exception ex)
            {
                Log("页面资源读取失败 " + path + "：" + ex.Message);
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(Encoding.UTF8.GetBytes("error")), 500, "Internal Error",
                    "Content-Type: text/plain; charset=utf-8\r\n");
            }
        }

        /// <summary>/static/… 直接读 engine\static（图标等），不经 Python。</summary>
        private void ServeLocalStatic(string pathAndQuery, CoreWebView2WebResourceRequestedEventArgs e)
        {
            string rel = pathAndQuery.Substring("/static/".Length);
            int q = rel.IndexOf('?');
            if (q >= 0) rel = rel.Substring(0, q);
            string baseDir = Path.GetFullPath(Path.Combine(Root, "engine", "static"));
            string full = Path.GetFullPath(Path.Combine(baseDir, rel.Replace('/', Path.DirectorySeparatorChar)));
            if (!full.StartsWith(baseDir, StringComparison.OrdinalIgnoreCase) || !File.Exists(full))
            {
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(Encoding.UTF8.GetBytes("not found")), 404, "Not Found",
                    "Content-Type: text/plain; charset=utf-8\r\n");
                return;
            }
            try
            {
                byte[] body = File.ReadAllBytes(full);
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(body), 200, "OK",
                    "Content-Type: " + GuessContentType(Path.GetExtension(full)) + "\r\nCache-Control: no-store\r\n");
            }
            catch (Exception ex)
            {
                Log("静态资源读取失败 " + rel + "：" + ex.Message);
                e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                    new MemoryStream(Encoding.UTF8.GetBytes("error")), 500, "Internal Error",
                    "Content-Type: text/plain; charset=utf-8\r\n");
            }
        }

        private static string GuessContentType(string ext)
        {
            switch ((ext ?? "").ToLowerInvariant())
            {
                case ".png": return "image/png";
                case ".jpg":
                case ".jpeg": return "image/jpeg";
                case ".gif": return "image/gif";
                case ".webp": return "image/webp";
                case ".svg": return "image/svg+xml";
                case ".ico": return "image/x-icon";
                case ".html":
                case ".htm": return "text/html; charset=utf-8";
                case ".js":
                case ".mjs": return "text/javascript; charset=utf-8";
                case ".css": return "text/css; charset=utf-8";
                case ".json": return "application/json; charset=utf-8";
                case ".woff2": return "font/woff2";
                case ".txt": return "text/plain; charset=utf-8";
                default: return "application/octet-stream";
            }
        }

        /// <summary>
        /// 把页面发往 https://resinloop.local/api/* 的请求代转到引擎端口。
        /// 必须显式 req.Proxy = null：系统代理会拦截 loopback，返回空响应。
        /// </summary>
        private void ForwardToEngine(string pathAndQuery, string method, CoreWebView2WebResourceRequestedEventArgs e)
        {
            CoreWebView2Deferral deferral = e.GetDeferral();

            byte[] reqBody = null;
            string reqType = null;
            try
            {
                if (e.Request.Content != null)
                {
                    using (var ms = new MemoryStream())
                    {
                        e.Request.Content.CopyTo(ms);
                        reqBody = ms.ToArray();
                    }
                    if (reqBody.Length > 0) reqType = e.Request.Headers.GetHeader("Content-Type");
                }
            }
            catch (Exception ex)
            {
                Log("读取请求体失败：" + ex.Message);
            }

            int port = _port;
            Task.Run(() =>
            {
                int code = 502;
                string reason = "Bad Gateway";
                byte[] body = Encoding.UTF8.GetBytes("{\"ok\":false,\"message\":\"引擎不可达\"}");
                string ctype = "application/json; charset=utf-8";
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + pathAndQuery);
                    req.Method = method;
                    req.Proxy = null;
                    req.Timeout = 30000;
                    req.ReadWriteTimeout = 30000;
                    req.KeepAlive = false;
                    req.Accept = "application/json";
                    if (reqBody != null && reqBody.Length > 0)
                    {
                        req.ContentType = string.IsNullOrEmpty(reqType) ? "application/json" : reqType;
                        req.ContentLength = reqBody.Length;
                        using (var rs = req.GetRequestStream()) rs.Write(reqBody, 0, reqBody.Length);
                    }
                    using (var resp = (HttpWebResponse)req.GetResponse())
                    {
                        code = (int)resp.StatusCode;
                        reason = resp.StatusDescription;
                        if (!string.IsNullOrEmpty(resp.ContentType)) ctype = resp.ContentType;
                        using (var rs = resp.GetResponseStream())
                        using (var ms = new MemoryStream())
                        {
                            if (rs != null) rs.CopyTo(ms);
                            body = ms.ToArray();
                        }
                    }
                }
                catch (WebException wex)
                {
                    var hr = wex.Response as HttpWebResponse;
                    if (hr != null)
                    {
                        code = (int)hr.StatusCode;
                        reason = hr.StatusDescription;
                        if (!string.IsNullOrEmpty(hr.ContentType)) ctype = hr.ContentType;
                        using (var rs = hr.GetResponseStream())
                        using (var ms = new MemoryStream())
                        {
                            if (rs != null) rs.CopyTo(ms);
                            body = ms.ToArray();
                        }
                        hr.Dispose();
                    }
                    else
                    {
                        Log("转发 " + pathAndQuery + " 失败：" + wex.Message);
                    }
                }
                catch (Exception ex)
                {
                    Log("转发 " + pathAndQuery + " 异常：" + ex.Message);
                }

                try
                {
                    _web.BeginInvoke(new Action(() =>
                    {
                        try
                        {
                            e.Response = _web.CoreWebView2.Environment.CreateWebResourceResponse(
                                new MemoryStream(body), code, reason,
                                "Content-Type: " + ctype + "\r\nCache-Control: no-store\r\n");
                        }
                        catch (Exception ex)
                        {
                            Log("构造响应失败：" + ex.Message);
                        }
                        finally
                        {
                            deferral.Complete();
                        }
                    }));
                }
                catch (Exception ex)
                {
                    Log("回投失败：" + ex.Message);
                    deferral.Complete();
                }
            });
        }

        private int PickPort()
        {
            // ① 已有 ResinLoop 实例的端口直接复用（8765 优先，8899 次之）
            foreach (int p in new[] { 8765, 8899 })
                if (IsResinLoopAlive(p)) return p;
            // ② 全空闲 → 新引擎挑一个（8765 优先，被占就往后走）
            foreach (int p in new[] { 8765, 8899, 8900, 8901, 8902, 8903, 8904, 8905, 8906, 8907, 8908, 8909 })
                if (TcpFree(p)) return p;
            return -1;
        }

        private int PickPortWithOverride()
        {
            if (_forcedPort.HasValue)
            {
                int p = _forcedPort.Value;
                if (IsResinLoopAlive(p)) return p;              // 该端口已有 ResinLoop → 复用
                for (int q = p; q < p + 12; q++)                // 空闲或被非 ResinLoop 占用 → 从它开始向后找
                    if (TcpFree(q)) return q;
                return -1;
            }
            return PickPort();
        }

        private void StartEngine(int port)
        {
            var psi = new ProcessStartInfo
            {
                FileName = EnginePython,
                Arguments = "\"" + EngineApp + "\" --port " + port,
                WorkingDirectory = Path.GetDirectoryName(EngineApp),
                UseShellExecute = false,
                CreateNoWindow = true,          // pythonw 本就无窗，双保险防任何黑框
            };
            _engine = Process.Start(psi);
            AttachJobObject(_engine);           // 内核级收尸：壳死 = 引擎死
        }

        private static bool WaitEngineReady(int port, int timeoutMs)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                if (IsResinLoopAlive(port)) return true;
                Thread.Sleep(250);
            }
            return false;
        }

        // ------------------------------------------------------------ WebView2 沉浸化

        private void OnWebInitDone(object sender, CoreWebView2InitializationCompletedEventArgs e)
        {
            if (!e.IsSuccess) { Log("WebView2 初始化失败：" + e.InitializationException?.Message); return; }
            var s = _web.CoreWebView2.Settings;
            s.AreDefaultContextMenusEnabled = false;      // 屏蔽网页右键菜单
            s.AreDevToolsEnabled = false;
            s.IsStatusBarEnabled = false;
            s.AreBrowserAcceleratorKeysEnabled = false;   // 屏蔽 Ctrl+F / F12 等浏览器手感
            s.IsZoomControlEnabled = false;
            s.IsBuiltInErrorPageEnabled = true;
            // 隐藏滚动条（页面深浅色由用户自己选，见 UI 侧栏底部的三态按钮）
            _web.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                "(function(){var apply=function(){var st=document.createElement('style');" +
                "st.textContent='::-webkit-scrollbar{width:0;height:0}::-webkit-scrollbar-thumb{background:transparent}';" +
                // 注意：这里原本还有一句 removeItem('rl-theme')，用来保证页面主题
                // 与壳同口径。现已改为允许用户在侧栏底部手动切换深浅配色（auto/dark/light，
                // 偏好存在 localStorage 里），所以不再清理，否则每次启动都会抹掉用户的选择。
                "document.documentElement.appendChild(st);};" +
                // 文档创建时 documentElement 可能还不存在，直接 appendChild 会抛 null 引用
                "if(document.documentElement){apply()}else{document.addEventListener('DOMContentLoaded',apply)}" +
                "})();");
            _web.CoreWebView2.NewWindowRequested += (s2, e2) => { e2.Handled = true; };  // 屏蔽弹新窗
            Log("WebView2 就绪：右键菜单/DevTools/加速键已关，滚动条已隐藏");
        }

        private static bool IsWindowsDark()
        {
            try
            {
                using (var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                {
                    object v = key == null ? null : key.GetValue("AppsUseLightTheme");
                    if (v is int) return ((int)v) == 0;
                }
            }
            catch { }
            return false;
        }

        // ------------------------------------------------------------ 探测

        private static bool IsResinLoopAlive(int port)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/env");
                req.Timeout = 800;
                req.ReadWriteTimeout = 800;
                req.Proxy = null;
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                {
                    return sr.ReadToEnd().Contains("channels");   // ResinLoop 特征字段
                }
            }
            catch { return false; }
        }

        private static string HttpJson(int port, string path, string method, string body, int timeoutMs)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + path);
                req.Method = method;
                req.Proxy = null;
                req.Timeout = timeoutMs;
                req.ReadWriteTimeout = timeoutMs;
                req.ServicePoint.Expect100Continue = false;
                if (method == "POST")
                {
                    byte[] bytes = Encoding.UTF8.GetBytes(body ?? "{}");
                    req.ContentType = "application/json; charset=utf-8";
                    req.ContentLength = bytes.Length;
                    using (var s = req.GetRequestStream()) s.Write(bytes, 0, bytes.Length);
                }
                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                    return sr.ReadToEnd();
            }
            catch { return ""; }
        }

        /// <summary>守护进程是否在跑：daemon 对象（"daemon" 后 400 字符窗口）内匹配 running:true。</summary>
        private static bool DaemonRunning(int port)
        {
            string body = HttpJson(port, "/api/schedule", "GET", null, 1500);
            int i = body.IndexOf("\"daemon\"");
            return i >= 0 && Regex.IsMatch(body.Substring(i, Math.Min(400, body.Length - i)),
                                            "\"running\"\\s*:\\s*true");
        }

        private void EnsureDaemon(int port)
        {
            if (DaemonRunning(port))
            {
                Log("守护进程已在运行，跳过拉起");
                return;
            }
            string resp = HttpJson(port, "/api/daemon/start", "POST", "{}", 10000);
            var ok = Regex.IsMatch(resp, "\"ok\"\\s*:\\s*true");
            // start_daemon() 内部自带最长 6 秒就绪等待：POST 超时返回空串时
            // daemon 可能其实已经起来了 —— 必须把 8 秒观察窗跑满，再统一判定。
            if (!ok && resp == "")
                Log("启动请求无响应（可能仍在启动），继续观察 8 秒");
            DateTime deadline = DateTime.UtcNow.AddSeconds(8);
            while (DateTime.UtcNow < deadline && !DaemonRunning(port))
                Thread.Sleep(500);
            if (DaemonRunning(port))
            {
                var m = Regex.Match(resp, "\"pid\"\\s*:\\s*(\\d+)");
                Log("守护进程已拉起 pid=" + (m.Success ? m.Groups[1].Value : "?"));
                return;
            }
            Log("守护进程拉起失败：" + (resp == "" ? "(无响应)" : resp));
            if (IsHandleCreated && !IsDisposed)
                BeginInvoke((Action)(() => _tray.ShowBalloonTip(3000, "ResinLoop",
                    "定时巡检未能启动，请到「定时任务」页手动启动", ToolTipIcon.Warning)));
        }

        private static bool TcpFree(int port)
        {
            try
            {
                var l = new TcpListener(IPAddress.Loopback, port);
                l.Start(); l.Stop();
                return true;
            }
            catch { return false; }
        }

        /// <summary>托盘「立即巡检」：经 /api/daemon/run-now 让 daemon 跑一轮，不阻塞 UI。</summary>
        private void RunManualCheck()
        {
            if (!IsHandleCreated || IsDisposed) return;   // D0 守卫：壳已不在就不发请求
            Task.Run(() =>
            {
                string resp = HttpJson(_port, "/api/daemon/run-now", "POST", "{}", 5000);
                bool ok = Regex.IsMatch(resp, "\"ok\"\\s*:\\s*true");
                if (IsHandleCreated && !IsDisposed)
                    BeginInvoke((Action)(() => _tray.ShowBalloonTip(3000, "ResinLoop",
                        ok ? "已开始巡检" : "巡检未能启动，请打开控制台查看",
                        ok ? ToolTipIcon.Info : ToolTipIcon.Warning)));
            });
        }

        // ------------------------------------------------------------ 生命周期

        private void ShowMain()
        {
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        private void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            Log("FormClosing 触发：reason=" + e.CloseReason + " cancel前=" + e.Cancel);
            // 点 X / 最小化：藏进托盘（Job 活着 = 引擎活着），只有「彻底退出」才真退
            if (_reallyExit || e.CloseReason == CloseReason.ApplicationExitCall
                || e.CloseReason == CloseReason.WindowsShutDown)
                return;
            e.Cancel = true;
            Hide();
            Log("已取消关闭并隐藏到托盘");
            if (!_trayHintShown)
            {
                _tray.ShowBalloonTip(2000, "ResinLoop", "已最小化到托盘，日常巡查继续运行。", ToolTipIcon.Info);
                _trayHintShown = true;
            }
        }

        private void ReallyExit()
        {
            _reallyExit = true;
            Log("壳彻底退出：开始收尸引擎");
            try
            {
                if (_engine != null && !_engine.HasExited)
                {
                    _engine.Kill();
                    _engine.WaitForExit(3000);
                    Log("引擎进程已结束 pid=" + _engine.Id);
                }
            }
            catch (Exception ex) { Log("收尸异常：" + ex.Message); }
            finally
            {
                if (_job != IntPtr.Zero) { CloseHandle(_job); _job = IntPtr.Zero; }  // Job close 连带 daemon
            }
            _tray.Visible = false;
            Application.Exit();
        }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            if (_startMinimized) Hide();
        }

        // ------------------------------------------------------------ Job Object（内核级收尸）

        private void AttachJobObject(Process child)
        {
            try
            {
                _job = CreateJobObject(IntPtr.Zero, null);
                var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                info.BasicLimitInformation.LimitFlags = 0x2000;   // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                int len = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
                IntPtr ptr = Marshal.AllocHGlobal(len);
                try
                {
                    Marshal.StructureToPtr(info, ptr, false);
                    SetInformationJobObject(_job, 9, ptr, len);   // JobObjectExtendedLimitInformation
                }
                finally { Marshal.FreeHGlobal(ptr); }
                AssignProcessToJobObject(_job, child.Handle);
                Log("引擎已挂入 Job Object（KILL_ON_JOB_CLOSE）：壳异常退出也会被内核收尸");
            }
            catch (Exception ex) { Log("挂 Job 失败（不影响运行，仅失去兜底收尸）：" + ex.Message); }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr a, string lpName);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr hJob, int infoType, IntPtr lpInfo, int cbInfo);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        // ------------------------------------------------------------ 杂项

        private static void Log(string msg)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(ShellLog));
                File.AppendAllText(ShellLog,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + Environment.NewLine);
            }
            catch { }
        }
    }
}
