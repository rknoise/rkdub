// rkdub telepítő (egyetlen exe): a program fájljai beágyazva (payload.zip), a többit a tools\install.ps1 tölti le
// és állítja be (Python, PyTorch, csomagok, Ollama + fordítómodell, beszédmodellek, ikonok, eltávolító).
// Fordítás: tools\build_installer.ps1 (a Windows beépített .NET Framework 4 C#-fordítójával, külön eszköz nélkül).
// Parancssor (felügyelet nélküli telepítéshez): rkdub-telepito.exe /auto [/dir=C:\rkdub] [/noollama] [/nomodels] [/noshortcut]

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("rkdub telepítő")]
[assembly: AssemblyProduct("rkdub - helyi magyar szinkron")]
[assembly: AssemblyVersion("1.2.1.0")]
[assembly: AssemblyFileVersion("1.2.1.0")]

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

// a telepítés egy része (a tools\install.ps1 "##PLAN" soraiból)
class Part
{
    public const int Waiting = 0, Active = 1, Ready = 2, Skipped = 3;
    public string Id, Label, Note = "";
    public long Total, Done;
    public long Base = -1;      // az első jelentés: ennyi megvolt már (folytatott letöltés), a sebességbe nem számít
    public int State = Waiting;
}

class DoubleBufferedPanel : Panel
{
    public DoubleBufferedPanel() { DoubleBuffered = true; }
}

class SetupForm : Form
{
    const string FeedbackMail = "rknoise@gmail.com";
    const string WelcomeHu = "Helló! Ez a program RKnoise szüleménye.\r\n\r\n" +
        "Az rkdub magyarra szinkronizálja az angol (vagy más nyelvű) videókat és hangfájlokat, az eredeti beszélők " +
        "hangján, a háttérzene és a zajok megtartásával. Olyan, mint az ElevenLabs Dubbing, csak a saját gépeden, " +
        "a saját videókártyádon fut. Internet csak a telepítéshez kell.";
    const string WelcomeEn = "Hello! This program is RKnoise's creation.\r\n\r\n" +
        "rkdub dubs English (or other language) videos and audio files into Hungarian in the original speakers' " +
        "voices, keeping the background music and noises. It works like ElevenLabs Dubbing, but it runs on your " +
        "own computer and graphics card. You only need internet for the installation.";
    const string FeedbackHu = "Visszajelzést az " + FeedbackMail + " címre kérek, a tárgyba írd: dubby";
    const string FeedbackEn = "Please send feedback to " + FeedbackMail + " with the subject: dubby";

    static readonly CultureInfo Hu = CultureInfo.GetCultureInfo("hu-HU");
    static readonly Color Bg = ColorTranslator.FromHtml("#090d16");
    static readonly Color Card = ColorTranslator.FromHtml("#111827");
    static readonly Color Border = ColorTranslator.FromHtml("#1f293d");
    static readonly Color Orange = ColorTranslator.FromHtml("#ff6b00");
    static readonly Color Txt = ColorTranslator.FromHtml("#e5e7eb");
    static readonly Color Muted = ColorTranslator.FromHtml("#9ca3af");
    static readonly Color Green = ColorTranslator.FromHtml("#34d399");
    static readonly Color Amber = ColorTranslator.FromHtml("#fbbf24");
    static readonly Color Red = ColorTranslator.FromHtml("#f87171");

    public int ExitCode = 0;
    bool auto, running, finished, success;
    TextBox dirBox, logBox;
    Label diskLabel, gpuLabel, hintLabel, stepLabel, pathWarn, nowLabel, partLabel, totalLabel, statsLabel;
    CheckBox chkOllama, chkModels, chkShortcut;
    Button installBtn, browseBtn, closeBtn;
    Panel barBack, barFill, partBack, partFill, listPanel;
    readonly List<Control> setupView = new List<Control>(), installView = new List<Control>();
    Process proc;
    StreamWriter logFile;
    string gpuName = "";
    int gpuMiB = 0, fakeVram = 0;

    // folyamatjelző: minden mező a felület szálán változik
    readonly List<Part> parts = new List<Part>();
    Part cur;
    readonly Stopwatch clock = new Stopwatch();
    readonly List<KeyValuePair<double, long>> samples = new List<KeyValuePair<double, long>>();   // (letöltési idő, bájt)
    double activeSeconds, lastTick, lastByteAt;
    long lastNet;
    System.Windows.Forms.Timer ticker;

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
            else if (l.StartsWith("/vram=")) int.TryParse(a.Substring(6), out fakeVram);   // fejlesztéshez: más méretű videókártya
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
        chkModels = Chk("Beszéd- és hangmodellek letöltése most (kb. 9 GB; különben az első szinkronnál)", 24, 258, !noModels);
        chkShortcut = Chk("Asztali ikon (a Start menübe mindenképp bekerül)", 24, 284, !noShortcut);
        foreach (CheckBox c in new[] { chkOllama, chkModels, chkShortcut })
        {
            c.CheckedChanged += delegate { UpdateInfo(); };
            Controls.Add(c);
        }

        hintLabel = Lbl("", 24, 318, Muted);
        Controls.Add(hintLabel);

        // --- üdvözlés
        Panel welcome = new Panel { Left = 24, Top = 346, Width = 772, Height = 246, BackColor = Card };
        welcome.Paint += delegate(object s, PaintEventArgs e)
        {
            e.Graphics.DrawRectangle(new Pen(Border), 0, 0, welcome.Width - 1, welcome.Height - 1);
            e.Graphics.DrawLine(new Pen(Border), welcome.Width / 2, 14, welcome.Width / 2, welcome.Height - 14);
        };
        welcome.Controls.Add(WelcomeText(WelcomeHu, 16));
        welcome.Controls.Add(WelcomeText(WelcomeEn, 400));
        welcome.Controls.Add(FeedbackLink(FeedbackHu, 16));
        welcome.Controls.Add(FeedbackLink(FeedbackEn, 400));
        Controls.Add(welcome);

        foreach (Control c in new Control[] { dirBox, browseBtn, pathWarn, gpuLabel, diskLabel, chkOllama, chkModels,
                                              chkShortcut, hintLabel, welcome })
            setupView.Add(c);
        foreach (Control c in Controls)
            if (c is Label) setupView.Add(c);       // a mappa fölötti felirat is

        // --- folyamatjelző és napló (a Telepítés gomb után ez váltja az előzőket)
        nowLabel = Lbl("A program fájljainak kicsomagolása...", 24, 92, Color.White);
        nowLabel.Font = new Font("Segoe UI", 11.5f, FontStyle.Bold);
        partLabel = new Label { Left = 496, Top = 96, Width = 300, Height = 20, TextAlign = ContentAlignment.TopRight,
            ForeColor = Txt, BackColor = Color.Transparent };
        partBack = new Panel { Left = 24, Top = 122, Width = 772, Height = 8, BackColor = Card };
        partFill = new Panel { Left = 0, Top = 0, Width = 0, Height = 8, BackColor = Amber };
        partBack.Controls.Add(partFill);
        totalLabel = Lbl("Teljes folyamat", 24, 142, Txt);
        barBack = new Panel { Left = 24, Top = 166, Width = 772, Height = 14, BackColor = Card };
        barFill = new Panel { Left = 0, Top = 0, Width = 0, Height = 14, BackColor = Orange };
        barBack.Controls.Add(barFill);
        statsLabel = Lbl("", 24, 188, Muted);
        stepLabel = new Label { Left = 24, Top = 212, Width = 772, Height = 40, ForeColor = Txt, BackColor = Color.Transparent };
        listPanel = new DoubleBufferedPanel { Left = 24, Top = 256, Width = 390, Height = 336, BackColor = Card };
        listPanel.Paint += PaintParts;
        logBox = new TextBox { Left = 424, Top = 256, Width = 372, Height = 336, Multiline = true, ReadOnly = true,
            ScrollBars = ScrollBars.Vertical, BackColor = ColorTranslator.FromHtml("#060911"), ForeColor = Muted,
            BorderStyle = BorderStyle.FixedSingle, Font = new Font("Consolas", 8.5f), WordWrap = true };
        foreach (Control c in new Control[] { nowLabel, partLabel, partBack, totalLabel, barBack, statsLabel, stepLabel,
                                              listPanel, logBox })
        {
            c.Visible = false;
            installView.Add(c);
            Controls.Add(c);
        }
        ticker = new System.Windows.Forms.Timer { Interval = 500 };
        ticker.Tick += delegate { UpdateSpeed(); };

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

    Label WelcomeText(string t, int x)
    {
        return new Label { Text = t, Left = x, Top = 16, Width = 356, Height = 170, ForeColor = Txt, BackColor = Color.Transparent };
    }

    LinkLabel FeedbackLink(string t, int x)
    {
        LinkLabel l = new LinkLabel { Text = t, Left = x, Top = 190, Width = 356, Height = 42, BackColor = Color.Transparent,
            ForeColor = Muted, LinkColor = Orange, ActiveLinkColor = Amber, VisitedLinkColor = Orange,
            LinkBehavior = LinkBehavior.HoverUnderline };
        l.LinkArea = new LinkArea(t.IndexOf(FeedbackMail), FeedbackMail.Length);
        l.LinkClicked += delegate
        {
            try { Process.Start("mailto:" + FeedbackMail + "?subject=dubby"); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "rkdub", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        };
        return l;
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
        if (fakeVram > 0) gpuMiB = fakeVram;
    }

    string AutoModel()
    {
        if (gpuMiB >= 20000) return "gemma3:27b (kb. 17 GB)";
        if (gpuMiB >= 11000) return "gemma3:12b (kb. 8 GB)";
        return "gemma3:4b (kb. 3 GB)";
    }

    double AutoModelGb() { return gpuMiB >= 20000 ? 17.41 : gpuMiB >= 11000 ? 8.16 : 3.35; }

    static bool OllamaInstalled()
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return File.Exists(Path.Combine(local, "Programs", "Ollama", "ollama app.exe"));
    }

    double DownloadGb()
    {
        double gb = 3.85;                                           // uv, Python, PyTorch, csomagok
        if (chkOllama.Checked) gb += (OllamaInstalled() ? 0 : 1.68) + AutoModelGb();
        if (chkModels.Checked) gb += 9.11;
        return gb;
    }

    static string Gb(long bytes) { return (bytes / 1073741824.0).ToString("0.#") + " GB"; }

    // a letöltések mérete a szerverek szerint: 1 GB = 10^9 bájt
    static string Amount(long bytes)
    {
        if (bytes >= 1000000000) return (bytes / 1e9).ToString("0.00", Hu) + " GB";
        return (bytes / 1e6).ToString("0", Hu) + " MB";
    }

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
            // mért értékek: .venv 8 GB, modellek 9 GB, Ollama 3 GB; a csomagok telepítése közben a uv-gyorsítótár is 8 GB
            long need = 8L << 30;
            if (chkModels.Checked) need += 9L << 30;
            if (chkOllama.Checked)
            {
                need += (long)(AutoModelGb() * 1e9);
                if (!OllamaInstalled()) need += 3L << 30;
            }
            need = Math.Max(need, 16L << 30);
            string txt = "Szabad hely a " + root.TrimEnd('\\') + " meghajtón: " + Gb(d.AvailableFreeSpace) + " (kell kb. " + Gb(need) + ")";
            if (chkOllama.Checked)
                txt += "  ·  benne a fordítómodell: " + AutoModel();
            diskLabel.Text = txt;
            diskLabel.ForeColor = d.AvailableFreeSpace < need ? Red : Txt;
        }
        catch { diskLabel.Text = ""; }
        hintLabel.Text = "Kattints a Telepítés gombra. A letöltés kb. " + DownloadGb().ToString("0.0", Hu) +
            " GB, ami már megvan a gépen, azt nem tölti le újra.";
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
        installBtn.Text = "Folyamatban...";
        logBox.Clear();
        foreach (Control c in setupView) c.Visible = false;
        foreach (Control c in installView) c.Visible = true;
        parts.Clear();
        cur = null;
        samples.Clear();
        activeSeconds = lastTick = lastByteAt = 0;
        lastNet = 0;
        stepLabel.Text = "";
        stepLabel.ForeColor = Txt;
        nowLabel.Text = "A program fájljainak kicsomagolása...";
        clock.Restart();
        ticker.Start();
        RefreshProgress();
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
            si.EnvironmentVariables["RKDUB_GUI"] = "1";
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
        if (line.StartsWith("##DONE")) { success = true; return; }
        if (line.StartsWith("##FAIL")) { success = false; return; }
        if (line.StartsWith("##"))
        {
            string msg = line.Substring(2);
            UI(delegate { OnPartMessage(msg); });
            return;
        }
        Log(line);
    }

    Part Find(string id)
    {
        foreach (Part p in parts) if (p.Id == id) return p;
        return null;
    }

    // a sorok formátumát lásd a tools\install.ps1 elején
    void OnPartMessage(string msg)
    {
        string[] a = msg.Split(new[] { ' ' }, 4);
        if (a.Length < 2) return;
        if (a[0] == "PLAN")
        {
            long bytes;
            if (a.Length == 4 && long.TryParse(a[2], out bytes))
                parts.Add(new Part { Id = a[1], Total = bytes, Label = a[3] });
        }
        else
        {
            Part p = Find(a[1]);
            if (p == null) return;
            if (a[0] == "PART") { p.State = Part.Active; p.Note = ""; cur = p; }
            else if (a[0] == "SKIP")
            {
                p.State = Part.Skipped;
                if (cur == p) cur = null;
            }
            else if (a[0] == "END")
            {
                if (p.State != Part.Skipped) { p.State = Part.Ready; p.Done = p.Total; }
                p.Note = "";
                if (cur == p) cur = null;
            }
            else if (a[0] == "WORK") { p.Note = msg.Substring(a[0].Length + a[1].Length + 2); cur = p; }
            else if (a[0] == "GOT")
            {
                long done, total;
                if (!long.TryParse(a[2], out done)) return;
                if (a.Length > 3 && long.TryParse(a[3], out total) && total > 0) p.Total = total;
                if (p.Base < 0) p.Base = done;
                p.Done = Math.Min(done, p.Total);
                p.Note = "";
            }
        }
        RefreshProgress();
    }

    void GetTotals(out long done, out long total, out int ready)
    {
        done = total = 0;
        ready = 0;
        foreach (Part p in parts)
        {
            if (p.State == Part.Ready || p.State == Part.Skipped) ready++;
            if (p.State == Part.Skipped) continue;          // ami már megvolt, az nem letöltés
            done += p.Done;
            total += p.Total;
        }
    }

    void SetBar(Panel back, Panel fill, double ratio)
    {
        fill.Width = (int)(back.ClientSize.Width * Math.Max(0, Math.Min(1, ratio)));
    }

    void RefreshProgress()
    {
        long done, total;
        int ready;
        GetTotals(out done, out total, out ready);
        double ratio = total > 0 ? (double)done / total : 0;
        SetBar(barBack, barFill, ratio);
        if (parts.Count > 0)
            totalLabel.Text = "Teljes folyamat: " + (ratio * 100).ToString("0.0", Hu) + "%  ·  " + Amount(done) + " / " + Amount(total) +
                "  ·  " + ready + " / " + parts.Count + " rész kész";
        if (cur != null)
        {
            nowLabel.Text = cur.Label;
            partLabel.Text = cur.Note.Length > 0 ? cur.Note : cur.Total > 0 ? Amount(cur.Done) + " / " + Amount(cur.Total) +
                "  (" + (100.0 * cur.Done / cur.Total).ToString("0", Hu) + "%)" : "";
            SetBar(partBack, partFill, cur.Total > 0 ? (double)cur.Done / cur.Total : 0);
        }
        listPanel.Invalidate();
    }

    long NetBytes()
    {
        long sum = 0;
        foreach (Part p in parts) if (p.Base >= 0) sum += p.Done - p.Base;
        return sum;
    }

    static string Clock(double seconds)
    {
        int s = (int)seconds;
        return s >= 3600 ? (s / 3600) + ":" + (s / 60 % 60).ToString("00") + ":" + (s % 60).ToString("00")
                         : (s / 60) + ":" + (s % 60).ToString("00");
    }

    static string Remaining(double seconds)
    {
        int m = (int)Math.Ceiling(seconds / 60);
        return m >= 60 ? "kb. " + (m / 60) + " óra " + (m % 60) + " perc" : "kb. " + m + " perc";
    }

    double Rate(double window)
    {
        if (samples.Count == 0) return 0;
        KeyValuePair<double, long> last = samples[samples.Count - 1];
        foreach (KeyValuePair<double, long> s in samples)
            if (last.Key - s.Key <= window)
                return last.Key > s.Key ? (last.Value - s.Value) / (last.Key - s.Key) : 0;
        return 0;
    }

    // A mintákban csak a tényleges letöltéssel töltött idő számít: a kicsomagolás ne rontsa el a hátralévő időt.
    // Sebesség az utolsó 5, hátralévő idő az utolsó 30 másodpercből (a csomagok és a fájlok nem egyenletesen jönnek).
    void UpdateSpeed()
    {
        double now = clock.Elapsed.TotalSeconds;
        long net = NetBytes();
        if (net > lastNet)
        {
            activeSeconds += now - lastTick;
            samples.Add(new KeyValuePair<double, long>(activeSeconds, net));
            while (activeSeconds - samples[0].Key > 30) samples.RemoveAt(0);
            lastNet = net;
            lastByteAt = now;
        }
        lastTick = now;
        long done, total;
        int ready;
        GetTotals(out done, out total, out ready);
        double speed = now - lastByteAt < 3 ? Rate(5) : 0;
        string text = "Sebesség: " + (speed > 0 ? (speed / 1e6).ToString("0.0", Hu) + " MB/s" : "–");
        if (Rate(30) > 0 && total > done) text += "  ·  Hátra: " + Remaining((total - done) / Rate(30));
        statsLabel.Text = text + "  ·  Eltelt: " + Clock(now);
    }

    void PaintParts(object sender, PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        int n = parts.Count;
        if (n == 0) return;
        int w = listPanel.ClientSize.Width, pad = Font.Height / 2;
        int rowH = Math.Min(listPanel.ClientSize.Height / n, Font.Height * 2);
        using (Font symbols = new Font("Segoe UI Symbol", Font.Size))
        using (SolidBrush fill = new SolidBrush(Orange))
        using (SolidBrush row = new SolidBrush(Border))
            for (int i = 0; i < n; i++)
            {
                Part p = parts[i];
                Rectangle r = new Rectangle(0, i * rowH, w, rowH);
                bool active = p.State == Part.Active;
                if (active)
                {
                    g.FillRectangle(row, r);
                    if (p.Total > 0)
                        g.FillRectangle(fill, 0, r.Bottom - 2, (int)(w * Math.Min(1.0, (double)p.Done / p.Total)), 2);
                }
                string mark = active ? "▶" : p.State == Part.Waiting ? "○" : "✓";
                Color mc = active ? Orange : p.State == Part.Ready ? Green : Muted;
                Color tc = active ? Color.White : p.State == Part.Waiting ? Muted : Txt;
                string right = p.State == Part.Skipped ? "már megvan" : active && p.Total > 0 ?
                    (100.0 * p.Done / p.Total).ToString("0", Hu) + "%" : p.Total > 0 ? Amount(p.Total) : "";
                TextFormatFlags flags = TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.EndEllipsis;
                int rightW = pad * 12, left = pad * 3;
                TextRenderer.DrawText(g, mark, symbols, new Rectangle(pad, r.Top, left - pad, rowH), mc, flags);
                TextRenderer.DrawText(g, p.Label, Font, new Rectangle(left, r.Top, w - left - rightW, rowH), tc, flags);
                TextRenderer.DrawText(g, right, Font, new Rectangle(w - rightW - pad, r.Top, rightW, rowH), Muted, flags | TextFormatFlags.Right);
            }
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
            ticker.Stop();
            clock.Stop();
            cur = null;
            statsLabel.Text = "Eltelt: " + Clock(clock.Elapsed.TotalSeconds);
            if (ok)
            {
                barFill.Width = barBack.ClientSize.Width;
                partFill.Width = partBack.ClientSize.Width;
                nowLabel.Text = "Kész!";
                partLabel.Text = "";
                stepLabel.Text = "Az rkdub telepítve. Indítás: az asztali „rkdub” ikon, vagy az alábbi gomb.";
                stepLabel.ForeColor = Green;
                installBtn.Text = "rkdub indítása";
                installBtn.Enabled = true;
            }
            else
            {
                nowLabel.Text = "Megszakadt";
                partLabel.Text = "";
                stepLabel.Text = "Hiba: " + err + " Ellenőrizd az internetkapcsolatot, és próbáld újra: a kész részeket nem tölti le újra, " +
                    "a PyTorch, az Ollama, a fordítómodell és a BS-Roformer ott folytatódik, ahol abbamaradt.";
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
        if (!auto && MessageBox.Show(this, "Megszakítod a telepítést? Később folytatható: a kész részeket nem tölti le újra.",
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
