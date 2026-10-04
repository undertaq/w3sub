// Exercises the actual upstream primitive converter and production batch visitor.
using System;
using System.Collections.Generic;
using System.Reflection;
using Newtonsoft.Json.Linq;
using WolvenKit.CR2W;
using WolvenKit.CR2W.JSON;
using WolvenKit.CR2W.Types;

internal static class OpaqueByteRegression
{
    static int Main()
    {
        var walk = typeof(CR2WJsonTool).GetMethod("WalkNode", BindingFlags.Static | BindingFlags.NonPublic);
        var helper = Assembly.LoadFrom("WolvenKit.CLI.exe").GetType("WolvenKit.CLI.ReferenceBatch", true);
        var visit = helper.GetMethod("Visit", BindingFlags.Static | BindingFlags.NonPublic);
        // Use the production batch options. Fall back to the original options
        // only when reproducing RED against the previous helper.
        var optionFactory = helper.GetMethod("CreateReferenceOptions", BindingFlags.Static | BindingFlags.NonPublic);
        var options = optionFactory == null ? new CR2WJsonToolOptions() :
            (CR2WJsonToolOptions)optionFactory.Invoke(null, null);
        var bytes = new CByteArray(new CR2WFile(), null, "flatCompiledData") {
            // Non-CR2W magic returns NoCr2w and previously fell back to a scalar.
            Bytes = new byte[] { 1, 2, 3, 4, 5, 6, 7, 8 }
        };
        try {
            var converted = walk.Invoke(null, new object[] { bytes, "", 0, options });
            var node = JObject.FromObject(converted);
            if (node["_value"]?.Type != JTokenType.Bytes)
                throw new Exception("Fixture did not exercise the byte-scalar fallback");
            visit.Invoke(null, new object[] { node, "CTestOwner", "flatCompiledData", new List<object>() });
            Console.Error.WriteLine("FAIL: unsupported embedded bytes were accepted as complete");
            return 1;
        } catch (TargetInvocationException error) when (error.InnerException is FormatException) {
            if (!error.InnerException.Message.Contains("opaque byte primitive")) throw;
            Console.WriteLine("PASS: IByteSource fallback rejected: " + error.InnerException.Message);
        }
        // CGUID is a known fixed-size identifier, not IByteSource data; it must
        // remain parseable despite its byte[] primitive representation.
        var guid = new CGUID(new CR2WFile(), null, "GUID");
        try {
            var converted = walk.Invoke(null, new object[] { guid, "", 0, options });
            visit.Invoke(null, new object[] { JObject.FromObject(converted), "CTestOwner", "GUID", new List<object>() });
        } catch (TargetInvocationException error) {
            Console.Error.WriteLine("FAIL: supported fixed-size GUID was rejected: " + error.InnerException.Message);
            return 1;
        }
        Console.WriteLine("PASS: supported fixed-size CGUID retained");
        return 0;
    }
}
