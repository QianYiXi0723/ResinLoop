using System;
using System.Threading;
using System.Windows.Forms;

namespace ResinLoop
{
    internal static class Program
    {
        /// <summary>应用入口。--tray：开机自启用，启动即藏进托盘不弹窗。</summary>
        [STAThread]
        private static void Main(string[] args)
        {
            bool createdNew;
            using (var mutex = new Mutex(true, "ResinLoop.Shell.Mutex", out createdNew))
            {
                if (!createdNew) return;   // 已有壳在跑：第二次双击直接退出（引擎复用由已有壳负责）
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
