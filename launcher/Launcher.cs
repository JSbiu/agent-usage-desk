using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

// GUI subsystem: Native Messaging has binary pipes and no visible console.
internal static class Launcher
{
    private static void Forward(Stream source, Stream target)
    {
        var buffer = new byte[8192];
        int size;
        while ((size = source.Read(buffer, 0, buffer.Length)) > 0)
        {
            target.Write(buffer, 0, size);
            target.Flush(); // Chrome keeps stdin open while waiting for a response.
        }
    }

    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string arguments = "-B -u " + Quote(Path.Combine(root, "native", "entry.py"));
            foreach (string arg in args) arguments += " " + Quote(arg);
            var info = new ProcessStartInfo(Path.Combine(root, "runtime", "python.exe"), arguments);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.WorkingDirectory = root;
            using (var child = Process.Start(info))
            {
                var input = new Thread(delegate()
                {
                    try { Forward(Console.OpenStandardInput(), child.StandardInput.BaseStream); }
                    catch (IOException) { }
                    finally { try { child.StandardInput.Close(); } catch (IOException) { } }
                });
                input.IsBackground = true;
                input.Start();
                var error = new Thread(delegate()
                {
                    try { Forward(child.StandardError.BaseStream, Console.OpenStandardError()); }
                    catch (IOException) { }
                });
                error.IsBackground = true;
                error.Start();
                Forward(child.StandardOutput.BaseStream, Console.OpenStandardOutput());
                child.WaitForExit();
                error.Join(1000);
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            byte[] message = Encoding.UTF8.GetBytes("Codex Monitor launcher: " + error.Message + "\n");
            Console.OpenStandardError().Write(message, 0, message.Length);
            return 1;
        }
    }
}
