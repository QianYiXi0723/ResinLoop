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

        public MainForm(bool startMinimized, int? forcedPort)
        {
            _startMinimized = startMinimized;
            _forcedPort = forcedPort;

            Text = "ResinLoop · 日常巡查";
            // 每日报表三卡（本周汇总／游戏概览／最近 7 天）纵向分摊高度，窗口太矮时
            // 「游戏概览」会被压成只能露出半张卡的小框。默认给足 1280×920，
            // 同时按主屏工作区夹住上限 —— 1366 高的本子也不会撑爆屏幕被裁。
            const int wantW = 1280, wantH = 920;
            Rectangle wa = Screen.PrimaryScreen != null
                ? Screen.PrimaryScreen.WorkingArea
                : new Rectangle(0, 0, wantW, wantH);
            Size = new Size(Math.Min(wantW, Math.Max(960, wa.Width - 40)),
                            Math.Min(wantH, Math.Max(600, wa.Height - 40)));
            MinimumSize = new Size(Math.Min(1024, Size.Width), Math.Min(700, Size.Height));
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
                _web.CoreWebView2.Navigate(_url);
                Log("壳加载 " + _url);
            }
            catch (Exception ex)
            {
                Log("启动异常：" + ex);
                MessageBox.Show("启动失败：" + ex.Message);
            }
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
                "const st=document.createElement('style');" +
                "st.textContent='::-webkit-scrollbar{width:0;height:0}::-webkit-scrollbar-thumb{background:transparent}';" +
                // 注意：这里原本还有一句 removeItem('rl-theme')，用来保证页面主题
                // 与壳同口径。现已改为允许用户在侧栏底部手动切换深浅配色（auto/dark/light，
                // 偏好存在 localStorage 里），所以不再清理，否则每次启动都会抹掉用户的选择。
                "document.documentElement.appendChild(st);");
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
