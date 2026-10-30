using System;
using System.Threading;
using System.Windows.Forms;

namespace ResinLoop
{
    internal static class Program
    {
        /// <summary>应用入口。--tray：开机自启用，启动即藏进托盘不弹窗。
        /// --allow-multi：仅构建门禁用，允许与用户正在运行的壳并存（跳过单实例互斥）。</summary>
        [STAThread]
        private static void Main(string[] args)
        {
            // 单实例互斥：第二次双击直接退出（引擎复用由已有壳负责）。
            // 门禁/自动化用 --allow-multi 起探针实例，否则用户开着壳就永远构建不过。
            bool allowMulti = args != null && Array.IndexOf(args, "--allow-multi") >= 0;
            string mutexName = allowMulti
                ? "ResinLoop.Shell.Mutex.Probe." + System.Diagnostics.Process.GetCurrentProcess().Id
                : "ResinLoop.Shell.Mutex";
            bool createdNew;
            using (var mutex = new Mutex(true, mutexName, out createdNew))
            {
                if (!createdNew) return;
                bool startMinimized = args != null && Array.IndexOf(args, "--tray") >= 0;
                int? forcedPort = null;
                if (args != null)
                    for (int i = 0; i < args.Length - 1; i++)
                        if (args[i] == "--port" && int.TryParse(args[i + 1], out int p))
                            forcedPort = p;
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.ThreadException += (s, e) =>
                    System.IO.File.AppendAllText(
                        System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs", "shell.log"),
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  UI线程异常：" + e.Exception + Environment.NewLine);
                AppDomain.CurrentDomain.UnhandledException += (s, e) =>
                    System.IO.File.AppendAllText(
                        System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs", "shell.log"),
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  未处理异常：" + e.ExceptionObject + Environment.NewLine);
                Application.Run(new MainForm(startMinimized, forcedPort));
                System.IO.File.AppendAllText(
                    System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs", "shell.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  Application.Run 已返回（主窗体真正关闭）" + Environment.NewLine);
            }
        }
    }
}
