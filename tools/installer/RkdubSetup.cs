// rkdub telepítő (egyetlen exe): a program fájljai beágyazva (payload.zip), a többit a tools\install.ps1 tölti le
// és állítja be (Python, PyTorch, csomagok, Ollama + fordítómodell, beszédmodellek, ikonok, eltávolító).
// Fordítás: tools\build_installer.ps1 (a Windows beépített .NET Framework 4 C#-fordítójával, külön eszköz nélkül).
// Parancssor (felügyelet nélküli telepítéshez): rkdub-telepito.exe /auto [/dir=C:\rkdub] [/noollama] [/nomodels] [/noshortcut]

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("rkdub telepítő")]
[assembly: AssemblyProduct("rkdub - helyi magyar szinkron")]
[assembly: AssemblyVersion("1.1.0.0")]
[assembly: AssemblyFileVersion("1.1.0.0")]

static class Program
{
    [DllImport("user32.dll")]
    static extern bool SetProcessDPIAware();

    [STAThread]
    static int Main(string[] args)
    {
        try { SetProcessDPIAware(); } catch { }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        SetupForm f = new SetupForm(args);
        Application.Run(f);
        return f.ExitCode;
    }
}

class SetupForm : Form
{
    static readonly Color Bg = ColorTranslator.FromHtml("#090d16");
    static readonly Color Card = ColorTranslator.FromHtml("#111827");
    static readonly Color Border = ColorTranslator.FromHtml("#1f293d");
    static readonly Color Orange = ColorTranslator.FromHtml("#ff6b00");
    static readonly Color Txt = ColorTranslator.FromHtml("#e5e7eb");
    static readonly Color Muted = ColorTranslator.FromHtml("#9ca3af");
    static readonly Color Green = ColorTranslator.FromHtml("#34d399");
    static readonly Color Amber = ColorTranslator.FromHtml("#fbbf24");
    static readonly Color Red = ColorTranslator.FromHtml("#f87171");

    // a lépések súlya a teljes folyamatjelzőn (a nagy letöltések többet érnek)
    static readonly int[] Weights = { 2, 3, 25, 15, 35, 15, 5 };

    public int ExitCode = 0;
    bool auto, running, finished, success;
    TextBox dirBox, logBox;
    Label diskLabel, gpuLabel, stepLabel, pathWarn;
    CheckBox chkOllama, chkModels, chkShortcut;
    Button installBtn, browseBtn, closeBtn;
    Panel barBack, barFill;
    Process proc;
    int curStep = 0;
    double curPct = 0;
    StreamWriter logFile;
    string gpuName = "";
    int gpuMiB = 0;

    public SetupForm(string[] args)
    {
        string dir = @"C:\rkdub";
        bool noOllama = false, noModels = false, noShortcut = false;
        string shot = null;
        foreach (string a in args)
        {
            string l = a.ToLowerInvariant();
            if (l == "/auto") auto = true;
            else if (l.StartsWith("/shot=")) shot = a.Substring(6).Trim('"');   // fejlesztéshez: az ablak képe PNG-be
            else if (l.StartsWith("/dir=")) dir = a.Substring(5).Trim('"');
            else if (l == "/noollama") noOllama = true;
            else if (l == "/nomodels") noModels = true;
            else if (l == "/noshortcut") noShortcut = true;
        }

        Text = "rkdub telepítő";
        Font = new Font("Segoe UI", 9.75f);
        BackColor = Bg;
        ForeColor = Txt;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(820, 660);
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

        // --- fejléc
        Panel head = new Panel { Left = 0, Top = 0, Width = 820, Height = 78, BackColor = Card };
        Controls.Add(head);
        PictureBox logo = new PictureBox { Left = 22, Top = 17, Width = 44, Height = 44, SizeMode = PictureBoxSizeMode.Zoom };
        try { logo.Image = Icon.ExtractAssociatedIcon(Application.ExecutablePath).ToBitmap(); } catch { }
        head.Controls.Add(logo);
        head.Controls.Add(new Label { Text = "rkdub telepítő", Left = 78, Top = 14, AutoSize = true,
            Font = new Font("Segoe UI", 15f, FontStyle.Bold), ForeColor = Color.White });
        head.Controls.Add(new Label { Text = "Helyi magyar szinkron az eredeti beszélők hangján – minden, ami az induláshoz kell, egy lépésben.",
            Left = 80, Top = 46, AutoSize = true, ForeColor = Muted });
        head.Paint += delegate(object s, PaintEventArgs e) { e.Graphics.DrawLine(new Pen(Border), 0, head.Height - 1, head.Width, head.Height - 1); };

        // --- telepítés helye
        Controls.Add(Lbl("Telepítés helye (ékezet nélküli útvonal ajánlott)", 24, 96, Muted));
        dirBox = new TextBox { Left = 24, Top = 120, Width = 660, Text = dir, BackColor = Card, ForeColor = Txt,
            BorderStyle = BorderStyle.FixedSingle, Font = new Font("Consolas", 10f) };
        dirBox.TextChanged += delegate { UpdateInfo(); };
        Controls.Add(dirBox);
        browseBtn = Btn("Tallózás", 694, 118, 102, 28, false);
        browseBtn.Click += delegate
        {
            using (FolderBrowserDialog d = new FolderBrowserDialog())
            {
                d.Description = "Válaszd ki, hova települjön az rkdub (pl. C:\\ vagy D:\\). Benne egy rkdub mappa jön létre.";
                if (d.ShowDialog(this) == DialogResult.OK)
                    dirBox.Text = Path.Combine(d.SelectedPath, "rkdub");
            }
        };
        Controls.Add(browseBtn);
        pathWarn = Lbl("", 24, 150, Amber);
        Controls.Add(pathWarn);

        // --- gép adatai
        gpuLabel = Lbl("Videókártya: keresés...", 24, 176, Txt);
        diskLabel = Lbl("", 24, 200, Txt);
        Controls.Add(gpuLabel);
        Controls.Add(diskLabel);

        // --- összetevők
        chkOllama = Chk("Fordító AI telepítése: Ollama + gemma3 fordítómodell (a videókártyádhoz illő méretben)", 24, 232, !noOllama);
        chkModels = Chk("Beszéd- és hangmodellek letöltése most (kb. 6 GB; különben az első szinkronnál)", 24, 258, !noModels);
        chkShortcut = Chk("Asztali ikon (a Start menübe mindenképp bekerül)", 24, 284, !noShortcut);
        foreach (CheckBox c in new[] { chkOllama, chkModels, chkShortcut })
        {
            c.CheckedChanged += delegate { UpdateInfo(); };
            Controls.Add(c);
        }

        // --- folyamatjelző és napló
        stepLabel = Lbl("Kattints a Telepítés gombra. Internetkapcsolat kell (kb. 10–30 GB letöltés, a választástól függően).", 24, 322, Muted);
        Controls.Add(stepLabel);
        barBack = new Panel { Left = 24, Top = 348, Width = 772, Height = 10, BackColor = Card };
        barFill = new Panel { Left = 0, Top = 0, Width = 0, Height = 10, BackColor = Orange };
        barBack.Controls.Add(barFill);
        Controls.Add(barBack);
        logBox = new TextBox { Left = 24, Top = 370, Width = 772, Height = 220, Multiline = true, ReadOnly = true,
            ScrollBars = ScrollBars.Vertical, BackColor = ColorTranslator.FromHtml("#060911"), ForeColor = Muted,
            BorderStyle = BorderStyle.FixedSingle, Font = new Font("Consolas", 9f), WordWrap = true };
        Controls.Add(logBox);

        // --- gombok
        installBtn = Btn("Telepítés", 556, 606, 130, 36, true);
        installBtn.Click += delegate { OnInstallClick(); };
        closeBtn = Btn("Bezárás", 696, 606, 100, 36, false);
        closeBtn.Click += delegate { Close(); };
        Controls.Add(installBtn);
        Controls.Add(closeBtn);

        FormClosing += OnClosing;
        Shown += delegate
        {
            DetectGpu();
            UpdateInfo();
            installBtn.Focus();                 // ne legyen kijelölve a mappa szövege
            if (shot != null)
            {
                Refresh();
                using (Bitmap bmp = new Bitmap(Width, Height))
                {
                    DrawToBitmap(bmp, new Rectangle(0, 0, Width, Height));
                    bmp.Save(shot, System.Drawing.Imaging.ImageFormat.Png);
                }
                Close();
                return;
            }
            if (auto) StartInstall();
        };
    }

    // ---------------------------------------------------------------- kis segédek a kinézethez
    Label Lbl(string t, int x, int y, Color c)
    {
        return new Label { Text = t, Left = x, Top = y, AutoSize = true, ForeColor = c, BackColor = Color.Transparent };
    }

    CheckBox Chk(string t, int x, int y, bool on)
    {
        return new CheckBox { Text = t, Left = x, Top = y, Width = 772, Height = 24, Checked = on, ForeColor = Txt,
            BackColor = Bg, FlatStyle = FlatStyle.Flat };
    }

    Button Btn(string t, int x, int y, int w, int h, bool primary)
    {
        Button b = new Button { Text = t, Left = x, Top = y, Width = w, Height = h, FlatStyle = FlatStyle.Flat,
            BackColor = primary ? Orange : Card, ForeColor = primary ? Color.White : Txt, Cursor = Cursors.Hand,
            Font = new Font("Segoe UI", 9.75f, primary ? FontStyle.Bold : FontStyle.Regular) };
        b.FlatAppearance.BorderColor = primary ? Orange : Border;
        b.FlatAppearance.BorderSize = 1;
        return b;
    }

    // ---------------------------------------------------------------- a gép adatai
    void DetectGpu()
    {
        try
        {
            ProcessStartInfo si = new ProcessStartInfo("nvidia-smi", "--query-gpu=name,memory.total --format=csv,noheader,nounits");
            si.UseShellExecute = false; si.RedirectStandardOutput = true; si.CreateNoWindow = true;
            using (Process p = Process.Start(si))
            {
                string line = p.StandardOutput.ReadLine() ?? "";
                p.WaitForExit(5000);
                string[] parts = line.Split(',');
                if (parts.Length >= 2)
                {
                    gpuName = parts[0].Trim();
                    int.TryParse(parts[1].Trim(), out gpuMiB);
                }
            }
        }
        catch { }
    }

    string AutoModel()
    {
        if (gpuMiB >= 20000) return "gemma3:27b (kb. 17 GB)";
        if (gpuMiB >= 11000) return "gemma3:12b (kb. 8 GB)";
        return "gemma3:4b (kb. 3 GB)";
    }

    static string Gb(long bytes) { return (bytes / 1073741824.0).ToString("0.#") + " GB"; }

    void UpdateInfo()
    {
        if (gpuName.Length > 0)
        {
            gpuLabel.Text = "Videókártya: " + gpuName + " · " + (gpuMiB / 1024.0).ToString("0") + " GB videómemória" +
                (chkOllama.Checked ? "   →   fordítómodell: " + AutoModel() : "");
            gpuLabel.ForeColor = Green;
        }
        else
        {
            gpuLabel.Text = "Nem találtam NVIDIA videokártyát: a program elindul, de nagyon lassú lesz (a szinkronhoz NVIDIA kell).";
            gpuLabel.ForeColor = Amber;
        }
        string dir = dirBox.Text.Trim();
        bool nonAscii = false;
        foreach (char ch in dir) if (ch > 127) nonAscii = true;
        pathWarn.Text = nonAscii ? "Figyelem: ékezetes betű van az útvonalban – ez hibát okozhat. Javaslat: C:\\rkdub" : "";
        try
        {
            string root = Path.GetPathRoot(Path.GetFullPath(dir));
            DriveInfo d = new DriveInfo(root);
            long need = 9L << 30;                                   // program + Python + PyTorch + csomagok
            if (chkModels.Checked) need += 6L << 30;
            string txt = "Szabad hely a " + root.TrimEnd('\\') + " meghajtón: " + Gb(d.AvailableFreeSpace) + " (kell kb. " + Gb(need) + ")";
            if (chkOllama.Checked)
                txt += "  ·  a fordítómodell az Ollama saját modellmappájába kerül";
            diskLabel.Text = txt;
            diskLabel.ForeColor = d.AvailableFreeSpace < need ? Red : Txt;
        }
        catch { diskLabel.Text = ""; }
    }

    // ---------------------------------------------------------------- telepítés
    void OnInstallClick()
    {
        if (finished && success) { LaunchApp(); return; }
        if (!running) StartInstall();
    }

    void StartInstall()
    {
        string dir;
        try { dir = Path.GetFullPath(dirBox.Text.Trim()); }
        catch { MessageBox.Show(this, "Érvénytelen mappa.", "rkdub", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        running = true; finished = false; success = false;
        installBtn.Enabled = false; browseBtn.Enabled = false; dirBox.Enabled = false;
        chkOllama.Enabled = chkModels.Enabled = chkShortcut.Enabled = false;
        closeBtn.Text = "Megszakítás";
        logBox.Clear();
        SetProgress(0, 0);
        string args = "";
        if (!chkOllama.Checked) args += " -NoOllama";
        if (!chkModels.Checked) args += " -NoModels";
        if (!chkShortcut.Checked) args += " -NoShortcut";
        Thread t = new Thread(delegate() { Work(dir, args); });
        t.IsBackground = true;
        t.Start();
    }

    void Work(string dir, string args)
    {
        try
        {
            Directory.CreateDirectory(dir);
            Directory.CreateDirectory(Path.Combine(dir, ".tools"));
            logFile = new StreamWriter(Path.Combine(dir, ".tools", "telepites.log"), true, new UTF8Encoding(false));
            logFile.AutoFlush = true;
            Log("rkdub telepítése: " + dir + "  (" + DateTime.Now.ToString("yyyy-MM-dd HH:mm") + ")");
            UI(delegate { stepLabel.Text = "A program fájljainak kicsomagolása..."; stepLabel.ForeColor = Txt; });
            int n = Extract(dir);
            Log("Kicsomagolva: " + n + " fájl");

            ProcessStartInfo si = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(dir, "tools", "install.ps1") + "\"" + args);
            si.UseShellExecute = false;
            si.RedirectStandardOutput = true;
            si.RedirectStandardError = true;
            si.CreateNoWindow = true;
            si.StandardOutputEncoding = Encoding.UTF8;
            si.StandardErrorEncoding = Encoding.UTF8;
            si.WorkingDirectory = dir;
            proc = new Process();
            proc.StartInfo = si;
            proc.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) OnLine(e.Data); };
            proc.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) OnLine(e.Data); };
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            proc.WaitForExit();
            int code = proc.ExitCode;
            proc = null;
            Finish(code == 0 && success, code == 0 && success ? "" : "A telepítés nem sikerült (kód: " + code + ").");
        }
        catch (Exception ex)
        {
            Log("HIBA: " + ex.Message);
            Finish(false, ex.Message);
        }
    }

    int Extract(string dir)
    {
        int n = 0;
        using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive zip = new ZipArchive(s, ZipArchiveMode.Read))
        {
            string full = Path.GetFullPath(dir) + Path.DirectorySeparatorChar;
            foreach (ZipArchiveEntry e in zip.Entries)
            {
                string target = Path.GetFullPath(Path.Combine(dir, e.FullName));
                if (!target.StartsWith(full, StringComparison.OrdinalIgnoreCase)) continue;   // biztonság: csak a mappába
                if (e.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                e.ExtractToFile(target, true);
                n++;
            }
        }
        return n;
    }

    void OnLine(string line)
    {
        if (line.StartsWith("##STEP "))
        {
            // ##STEP 3/7 szöveg
            string rest = line.Substring(7);
            int sp = rest.IndexOf(' ');
            string[] nm = rest.Substring(0, sp).Split('/');
            int.TryParse(nm[0], out curStep);
            string text = rest.Substring(sp + 1);
            UI(delegate { stepLabel.Text = "[" + nm[0] + "/" + nm[1] + "] " + text; stepLabel.ForeColor = Txt; });
            SetProgress(curStep, 0);
            Log(line.Substring(2));
            return;
        }
        if (line.StartsWith("##PCT "))
        {
            double p;
            if (double.TryParse(line.Substring(6), System.Globalization.NumberStyles.Any,
                                System.Globalization.CultureInfo.InvariantCulture, out p)) SetProgress(curStep, p);
            return;
        }
        if (line.StartsWith("##DONE")) { success = true; return; }
        if (line.StartsWith("##FAIL")) { success = false; return; }
        Log(line);
    }

    void SetProgress(int step, double pct)
    {
        curPct = pct;
        double done = 0;
        for (int i = 0; i < Weights.Length && i < step - 1; i++) done += Weights[i];
        if (step >= 1 && step <= Weights.Length) done += Weights[step - 1] * Math.Min(100, pct) / 100.0;
        double total = 0;
        foreach (int w in Weights) total += w;
        int width = (int)(barBack.Width * done / total);
        UI(delegate { barFill.Width = width; });
    }

    void Log(string line)
    {
        if (logFile != null) { try { logFile.WriteLine(line); } catch { } }
        UI(delegate
        {
            if (logBox.TextLength > 200000) logBox.Text = logBox.Text.Substring(100000);
            logBox.AppendText(line + Environment.NewLine);
        });
    }

    void Finish(bool ok, string err)
    {
        running = false; finished = true; success = ok;
        ExitCode = ok ? 0 : 1;
        if (logFile != null) { try { logFile.Close(); } catch { } logFile = null; }
        UI(delegate
        {
            closeBtn.Text = "Bezárás";
            if (ok)
            {
                barFill.Width = barBack.Width;
                stepLabel.Text = "Kész! Az rkdub telepítve. Indítás: az asztali „rkdub” ikon, vagy az alábbi gomb.";
                stepLabel.ForeColor = Green;
                installBtn.Text = "rkdub indítása";
                installBtn.Enabled = true;
            }
            else
            {
                stepLabel.Text = "Hiba: " + err + " Ellenőrizd az internetkapcsolatot, és próbáld újra (a letöltött részeket nem kezdi elölről).";
                stepLabel.ForeColor = Red;
                installBtn.Text = "Újra";
                installBtn.Enabled = true;
                browseBtn.Enabled = dirBox.Enabled = true;
                chkOllama.Enabled = chkModels.Enabled = chkShortcut.Enabled = true;
                finished = false;
            }
            if (auto)
            {
                System.Windows.Forms.Timer tm = new System.Windows.Forms.Timer { Interval = 1500 };
                tm.Tick += delegate { tm.Stop(); Close(); };
                tm.Start();
            }
        });
    }

    void LaunchApp()
    {
        try
        {
            string dir = Path.GetFullPath(dirBox.Text.Trim());
            ProcessStartInfo si = new ProcessStartInfo(Path.Combine(dir, "start.bat"));
            si.WorkingDirectory = dir;
            si.UseShellExecute = true;
            si.WindowStyle = ProcessWindowStyle.Minimized;
            Process.Start(si);
            Close();
        }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "rkdub", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
    }

    void OnClosing(object sender, FormClosingEventArgs e)
    {
        if (!running) return;
        if (!auto && MessageBox.Show(this, "Megszakítod a telepítést? Később folytatható: a már letöltött részeket nem kezdi elölről.",
                "rkdub", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
        {
            e.Cancel = true;
            return;
        }
        try
        {
            if (proc != null && !proc.HasExited)
            {
                // a PowerShell és minden gyermekfolyamata (uv, python, letöltések)
                Process.Start(new ProcessStartInfo("taskkill", "/T /F /PID " + proc.Id) { CreateNoWindow = true, UseShellExecute = false }).WaitForExit(5000);
            }
        }
        catch { }
        ExitCode = 2;
    }

    void UI(MethodInvoker a)
    {
        if (IsDisposed) return;
        try { if (InvokeRequired) BeginInvoke(a); else a(); } catch { }
    }
}
