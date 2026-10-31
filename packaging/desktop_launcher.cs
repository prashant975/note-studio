// NoteCraft.exe - copied from QBG One's desktop_launcher.cs (QBG_Studio_Vibecraft/backend/studio).
// The build replaces __APP_NAME__ with brand.APP_NAME. Starts the bundled Python on
// _internal\app\start_installed.py, shows "Starting..." until the server prints
// NOTESTUDIO_READY|<url>, then lives in the notification area. Exit code 0 from the
// server (an update, or Quit) ends the launcher quietly; anything else is shown.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class DesktopLauncher
{
    internal const string AppName = "__APP_NAME__";
    internal const string ReadyPrefix = "NOTESTUDIO_READY|";

    [STAThread]
    private static int Main(string[] args)
    {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string logs = Path.Combine(root, "logs");
        Directory.CreateDirectory(logs);
        bool check = args.Length == 1 && args[0] == "--check";
        if (check)
        {
            try
            {
                var start = StartInfo(root, true);
                using (var child = Process.Start(start))
                {
                    string output = child.StandardOutput.ReadToEnd();
                    string error = child.StandardError.ReadToEnd();
                    child.WaitForExit();
                    File.WriteAllText(Path.Combine(logs, "check.log"), output + error);
                    return child.ExitCode;
                }
            }
            catch (Exception error) { File.WriteAllText(Path.Combine(logs, "check.log"), error.ToString()); return 1; }
        }
        string identity;
        using (var sha = SHA256.Create())
            identity = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-", "");
        bool created;
        using (var mutex = new Mutex(true, "Local\\NoteCraft-Desktop-" + identity, out created))
        {
            if (!created)
            {
                string ready = Path.Combine(logs, "ready-url.txt");
                for (int i = 0; i < 30 && !File.Exists(ready); i++) Thread.Sleep(500);
                if (File.Exists(ready)) OpenUrl(File.ReadAllText(ready).Trim());
                else MessageBox.Show(AppName + " is still starting. Its startup window shows the progress.", AppName);
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new StudioContext(root, logs));
            return 0;
        }
    }

    internal static void OpenUrl(string url)
    {
        if (Environment.GetEnvironmentVariable("NOTE_STUDIO_NO_BROWSER") == "1") return;
        if (!url.StartsWith("http://127.0.0.1:", StringComparison.Ordinal)) return;
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
        catch (Exception error) { MessageBox.Show("Open this address in your browser: " + url + "\n\n" + error.Message, AppName); }
    }

    internal static ProcessStartInfo StartInfo(string root, bool check)
    {
        string internalDir = Path.Combine(root, "_internal");
        var start = new ProcessStartInfo(Path.Combine(internalDir, "python", "python.exe"));
        start.Arguments = "-u \"" + Path.Combine(internalDir, "app", "start_installed.py") + "\"" + (check ? " --check" : "");
        start.WorkingDirectory = Path.Combine(internalDir, "app");
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        // A Python set up on this computer must not leak into the bundled one.
        start.EnvironmentVariables.Remove("PYTHONHOME");
        start.EnvironmentVariables.Remove("PYTHONPATH");
        start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        return start;
    }
}

internal sealed class StudioContext : ApplicationContext
{
    private readonly Form window;
    private readonly NotifyIcon tray;
    private readonly Icon appIcon;
    private readonly string logs;
    private readonly object logLock = new object();
    private Process server;
    private bool serverStarted;
    private bool quitting;
    private string url = "";

    internal StudioContext(string root, string logFolder)
    {
        string name = DesktopLauncher.AppName;
        appIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        logs = logFolder;
        File.WriteAllText(Path.Combine(logs, "startup.log"), name + " startup " + DateTime.Now + Environment.NewLine);
        string ready = Path.Combine(logs, "ready-url.txt");
        if (File.Exists(ready)) File.Delete(ready);
        window = new Form { Text = name, ClientSize = new Size(470, 150),
            StartPosition = FormStartPosition.CenterScreen, FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false, MinimizeBox = false, Icon = appIcon };
        window.Controls.Add(new Label { Text = "Starting " + name + "...", Font = new Font("Segoe UI", 15), AutoSize = true, Left = 24, Top = 22 });
        window.Controls.Add(new Label { Text = "Your browser will open when the app is ready.", AutoSize = true, Left = 26, Top = 62 });
        window.Controls.Add(new ProgressBar { Style = ProgressBarStyle.Marquee, Left = 26, Top = 99, Width = 416, Height = 18 });
        window.FormClosing += delegate(object sender, FormClosingEventArgs e) { if (!quitting) Quit(); };
        var menu = new ContextMenuStrip();
        menu.Items.Add("Open " + name, null, delegate { DesktopLauncher.OpenUrl(url); });
        menu.Items.Add("Open startup log", null, delegate { Process.Start("notepad.exe", "\"" + Path.Combine(logs, "startup.log") + "\""); });
        menu.Items.Add("Quit " + name, null, delegate { Quit(); });
        tray = new NotifyIcon { Icon = appIcon, Text = name, ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += delegate { DesktopLauncher.OpenUrl(url); };
        window.Shown += delegate
        {
            try
            {
                server = new Process { StartInfo = DesktopLauncher.StartInfo(root, false), EnableRaisingEvents = true };
                server.OutputDataReceived += Output;
                server.ErrorDataReceived += Output;
                server.Exited += delegate { if (!quitting && window.IsHandleCreated) window.BeginInvoke((Action)Stopped); };
                serverStarted = server.Start();
                server.BeginOutputReadLine();
                server.BeginErrorReadLine();
            }
            catch (Exception error) { Fail(error.Message); }
        };
        window.Show();
    }

    private void Output(object sender, DataReceivedEventArgs e)
    {
        if (e.Data == null) return;
        lock (logLock) File.AppendAllText(Path.Combine(logs, "startup.log"), e.Data + Environment.NewLine);
        if (e.Data.StartsWith(DesktopLauncher.ReadyPrefix, StringComparison.Ordinal))
        {
            url = e.Data.Substring(DesktopLauncher.ReadyPrefix.Length);
            File.WriteAllText(Path.Combine(logs, "ready-url.txt"), url);
            if (!quitting) window.BeginInvoke((Action)delegate { window.Hide(); });
        }
    }

    private void Stopped()
    {
        if (quitting) return;
        if (server.ExitCode != 0) Fail(File.ReadAllText(Path.Combine(logs, "startup.log")));
        else Quit();
    }

    private void Fail(string message)
    {
        string name = DesktopLauncher.AppName;
        MessageBox.Show(window, name + " could not start.\n\n" + message + "\n\nStartup details: " + Path.Combine(logs, "startup.log"), name, MessageBoxButtons.OK, MessageBoxIcon.Error);
        Quit();
    }

    private void Quit()
    {
        if (quitting) return;
        quitting = true;
        if (serverStarted && server != null && !server.HasExited)
        {
            using (var stop = Process.Start(new ProcessStartInfo("taskkill.exe", "/PID " + server.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true })) stop.WaitForExit();
        }
        tray.Visible = false;
        tray.Dispose();
        window.Dispose();
        if (appIcon != null) appIcon.Dispose();
        ExitThread();
    }
}
