import os
import zipfile
import tempfile
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import logging
from logging.handlers import TimedRotatingFileHandler
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import openai

# Logging configuration
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"conversion_{datetime.now():%Y%m%d}.log")

logger = logging.getLogger("VB6Converter")
logger.setLevel(logging.DEBUG)

file_handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=7)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(console_handler)

# Environment variables
logger.info("Loading environment variables")
load_dotenv()

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

if not (AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_VERSION):
    logger.error("Required Azure OpenAI environment variables are missing")
    raise RuntimeError("Required Azure OpenAI environment variables are missing.")

client = openai.AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION
)
logger.info("Azure OpenAI client initialized")

class VB6Converter:
    def __init__(self):
        logger.info("Initializing VB6Converter")
        self.conversion_prompts = {
            'module_bas': """
Convert the following VB6 Module (.bas) file to C# for .NET 9 Worker Service.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Focus on:
1. Convert global variables to static properties in a Constants/GlobalVariables class
2. Convert functions/subroutines to static methods in service classes
3. Convert VB6-specific data types to C# equivalents (e.g., Long to uint, Byte to byte)
4. Convert error handling to try-catch blocks
5. Update file I/O to modern .NET (System.IO)
6. Convert COM objects to .NET equivalents or P/Invoke for Windows API
7. Handle J2534 API calls with proper [DllImport] attributes

VB6 Code:
{vb6_code}

Return JSON structure:
{{
  "Constants.cs": "C# code for constants class",
  "ModuleService.cs": "C# code for service class",
  "IModuleService.cs": "C# code for service interface"
}}
""",
            'class_cls': """
Convert the following VB6 Class (.cls) file to C# for .NET 9.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Focus on:
1. Convert properties to C# properties with get/set
2. Convert methods to C# methods
3. Convert events to C# events or delegates
4. Convert VB6 data types to C# equivalents (e.g., Long to uint, Byte to byte)
5. Handle initialization in constructor and cleanup in Dispose
6. Convert error handling to try-catch
7. Implement IDisposable for resource management
8. Handle J2534 API calls with proper [DllImport] attributes and structs (e.g., RX_structure, vciSCONFIG)

VB6 Code:
{vb6_code}

Return JSON structure:
{{
  "Class.cs": "C# code for the converted class"
}}
""",
            'class_chunk_converter': """
Convert this chunk of a VB6 .cls file (part {chunk_number} of {total_chunks}) to C# for .NET 9.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Class name: {class_name}
Focus on:
1. Maintain class structure and inheritance
2. Convert properties to C# properties with get/set
3. Convert methods to C# methods with proper signatures
4. Convert VB6 data types to C# equivalents (e.g., Long to uint, Byte to byte)
5. Handle J2534 API calls with [DllImport] and structs (e.g., RX_structure, vciSCONFIG)
6. Use [StructLayout] and [MarshalAs] for P/Invoke structs
7. Convert error handling to try-catch
8. Preserve method boundaries and context
9. Handle arrays and memory management for P/Invoke (e.g., Marshal.AllocHGlobal, Marshal.FreeHGlobal)
Previous context summary: {previous_context}
VB6 Code Chunk:
{vb6_code}

Return JSON structure:
{{
  "ClassChunk.cs": "converted C# code chunk",
  "ContextSummary": "brief context for next chunk including class structure, defined methods, structs, and J2534 API calls"
}}
""",
            'chunk_converter': """
Convert this chunk of a VB6 .bas file (part {chunk_number} of {total_chunks}) to C# for .NET 9.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Focus on:
1. Maintain variable scope and naming
2. Convert functions/subs to C# methods
3. Convert VB6 data types to C# equivalents (e.g., Long to uint, Byte to byte)
4. Handle J2534 API calls with proper [DllImport] attributes
5. Convert error handling to try-catch
6. Modern .NET patterns (e.g., async/await where applicable)
Previous context summary: {previous_context}
VB6 Code Chunk:
{vb6_code}

Return JSON structure:
{{
  "Chunk.cs": "converted C# code",
  "ContextSummary": "brief context for next chunk including defined methods and variables"
}}
"""
        }

    def chunk_large_file(self, content: str, max_chunk_size: int = 4000, file_type: str = "bas") -> List[str]:
        logger.debug(f"Chunking {file_type} file with size {len(content)}")
        lines = content.splitlines()
        chunks = []
        current_chunk = []
        current_size = 0

        if file_type == "cls":
            in_method = False
            in_struct = False
            method_start_keywords = ['Public Sub', 'Private Sub', 'Public Function', 'Private Function',
                                    'Property Get', 'Property Set', 'Property Let']
            method_end_keywords = ['End Sub', 'End Function', 'End Property']
            struct_start_keywords = ['Type ', 'Private Type', 'Public Type']
            struct_end_keywords = ['End Type']
            declare_keywords = ['Declare Function', 'Declare Sub']

            for line in lines:
                line_stripped = line.strip()

                if any(keyword in line for keyword in struct_start_keywords):
                    if current_size + len(line) > max_chunk_size and current_chunk and not in_method:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_size = len(line)
                        in_struct = True
                    else:
                        current_chunk.append(line)
                        current_size += len(line)
                        in_struct = True

                elif any(keyword in line for keyword in struct_end_keywords):
                    current_chunk.append(line)
                    current_size += len(line)
                    in_struct = False
                    if current_size > max_chunk_size * 0.8:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = []
                        current_size = 0

                elif any(keyword in line for keyword in declare_keywords):
                    if current_size + len(line) > max_chunk_size and current_chunk and not in_method and not in_struct:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_size = len(line)
                    else:
                        current_chunk.append(line)
                        current_size += len(line)

                elif any(keyword in line for keyword in method_start_keywords):
                    if current_size + len(line) > max_chunk_size and current_chunk and not in_struct:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_size = len(line)
                        in_method = True
                    else:
                        current_chunk.append(line)
                        current_size += len(line)
                        in_method = True

                elif any(keyword in line for keyword in method_end_keywords):
                    current_chunk.append(line)
                    current_size += len(line)
                    in_method = False
                    if current_size > max_chunk_size * 0.8:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = []
                        current_size = 0

                else:
                    if current_size + len(line) > max_chunk_size and current_chunk and not in_method and not in_struct:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_size = len(line)
                    else:
                        current_chunk.append(line)
                        current_size += len(line)

        else:
            for line in lines:
                if current_size + len(line) > max_chunk_size and current_chunk:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_size = len(line)
                else:
                    current_chunk.append(line)
                    current_size += len(line)

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        logger.debug(f"Created {len(chunks)} chunks for {file_type} file")
        return chunks

    def extract_class_name(self, content: str) -> str:
        lines = content.split('\n')
        for line in lines[:20]:
            if line.strip().startswith('Attribute VB_Name ='):
                match = re.search(r'Attribute VB_Name = "([^"]+)"', line)
                if match:
                    return match.group(1)
        for line in lines[:50]:
            if 'Class' in line and ('Public' in line or 'Private' in line):
                words = line.split()
                for i, word in enumerate(words):
                    if word == 'Class' and i + 1 < len(words):
                        return words[i + 1]
        return "UnknownClass"

    def classify_cls_purpose(self, content: str) -> str:
        """Classify the purpose of a .cls file based on content."""
        lines = content.splitlines()
        method_count = 0
        property_count = 0
        has_declare = False

        for line in lines:
            line_stripped = line.strip()
            if any(keyword in line_stripped for keyword in ['Public Sub', 'Private Sub', 'Public Function', 'Private Function']):
                method_count += 1
            elif any(keyword in line_stripped for keyword in ['Property Get', 'Property Let', 'Property Set']):
                property_count += 1
            elif 'Declare' in line_stripped and ('Function' in line_stripped or 'Sub' in line_stripped):
                has_declare = True

        # If J2534 API calls or multiple methods are present, classify as service
        if has_declare or method_count > 2:
            return "service"
        # If mostly properties, classify as model
        elif property_count > method_count:
            return "model"
        # Default to model for simple classes
        return "model"

    def sanitize_code(self, code: str) -> str:
        if not code or not isinstance(code, str):
            return ""
        code = re.sub(r'//.*?\n', '\n', code)
        code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
        code = re.sub(r'\n\s*\n', '\n', code)
        code = re.sub(r'```[a-zA-Z]*\n?', '', code)
        return code.strip()

    def extract_json_from_response(self, response_content: str) -> Dict[str, Any]:
        if not response_content:
            return {"error": "Empty response from API"}

        cleaned = re.sub(r'^```json\s*\n?', '', response_content, flags=re.MULTILINE)
        cleaned = re.sub(r'\n?```$', '', cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parse failed: {e}")

            try:
                fixed = cleaned.replace('\\"', '"').replace('\\\\', '\\')
                brace_count = 0
                start_idx = -1
                for i, char in enumerate(fixed):
                    if char == '{':
                        if brace_count == 0:
                            start_idx = i
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0 and start_idx != -1:
                            json_str = fixed[start_idx:i+1]
                            return json.loads(json_str)
            except json.JSONDecodeError:
                pass

            json_pattern = r'\{[\s\S]*\}'
            match = re.search(json_pattern, cleaned)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

            error_msg = f"Invalid JSON response: {cleaned[:200]}..."
            logger.error(error_msg)
            return {"error": error_msg}

    def call_azure_openai(self, prompt: str, max_tokens: int = 12000, retries: int = 3) -> Dict[str, Any]:
        logger.info("Calling Azure OpenAI API")

        for attempt in range(retries + 1):
            try:
                response = client.chat.completions.create(
                    model=AZURE_OPENAI_DEPLOYMENT,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are an expert VB6 to C# converter for .NET 9 Worker Services, specializing in J2534 API integration. "
                                "Return ONLY a valid JSON object. No markdown, no ```json wrapping, no comments, no explanations. "
                                "Ensure complete and properly formatted JSON, handling J2534 structs (e.g., RX_structure, vciSCONFIG) "
                                "with [StructLayout] and [MarshalAs], and P/Invoke declarations for BVTX4J32.dll and BVTX-VCI-RT-J.dll."
                            )
                        },
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=max_tokens,
                    temperature=0.1,
                    top_p=0.95
                )

                response_content = response.choices[0].message.content
                logger.debug(f"Received response (length: {len(response_content) if response_content else 0})")

                if not response_content:
                    if attempt < retries:
                        logger.info(f"Empty response, retrying (attempt {attempt + 2}/{retries + 1})")
                        continue
                    return {"error": "Empty response from Azure OpenAI API"}

                debug_file = f"logs/api_response_{datetime.now():%Y%m%d_%H%M%S}_{attempt + 1}.txt"
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(response_content)
                logger.debug(f"Saved raw API response to {debug_file}")

                parsed_response = self.extract_json_from_response(response_content)

                if "error" in parsed_response:
                    if attempt < retries:
                        logger.info(f"JSON parsing failed, retrying (attempt {attempt + 2}/{retries + 1})")
                        continue
                    return parsed_response

                expected_keys = ["Class.cs", "Constants.cs", "ModuleService.cs", "IModuleService.cs", "Chunk.cs", "ClassChunk.cs"]
                has_valid_key = any(key in parsed_response for key in expected_keys)

                if not has_valid_key:
                    if attempt < retries:
                        logger.info(f"Missing expected keys, retrying (attempt {attempt + 2}/{retries + 1})")
                        continue
                    return {"error": f"Missing expected keys. Found: {list(parsed_response.keys())}"}

                logger.info("Successfully parsed API response")
                return parsed_response

            except Exception as e:
                logger.error(f"Error in Azure OpenAI API call (attempt {attempt + 1}): {e}")
                if attempt < retries:
                    logger.info(f"Retrying due to exception (attempt {attempt + 2}/{retries + 1})")
                    continue
                return {"error": f"API call failed: {str(e)}"}

        return {"error": "Exhausted all retry attempts"}

    def convert_bas_file(self, content: str, filename: str, namespace: str) -> Dict[str, Any]:
        logger.info(f"Converting BAS file: {filename}")

        if not content or not content.strip():
            return {"error": f"Empty content in {filename}"}

        if len(content) > 15000:
            logger.debug("File is large, processing in chunks")
            chunks = self.chunk_large_file(content, max_chunk_size=5000, file_type="bas")
            parts, prev_ctx = [], ""

            for i, chunk in enumerate(chunks):
                logger.debug(f"Processing chunk {i+1}/{len(chunks)}")
                prompt = self.conversion_prompts['chunk_converter'].format(
                    chunk_number=i + 1,
                    total_chunks=len(chunks),
                    previous_context=prev_ctx,
                    vb6_code=chunk,
                    namespace=namespace
                )
                result = self.call_azure_openai(prompt, max_tokens=8000)

                if "error" in result:
                    logger.warning(f"Chunk {i+1} failed: {result['error']}")
                    continue

                parts.append(result)
                prev_ctx = result.get("ContextSummary", "")[:500]

            if not parts:
                return {"error": f"All chunks failed for {filename}"}

            return self.combine_converted_chunks(parts, filename, namespace)
        else:
            prompt = self.conversion_prompts['module_bas'].format(
                vb6_code=content,
                namespace=namespace
            )
            return self.call_azure_openai(prompt)

    def combine_converted_chunks(self, chunks: List[Dict[str, Any]], filename: str, namespace: str) -> Dict[str, Any]:
        logger.info(f"Combining {len(chunks)} chunks for {filename}")

        combine_prompt = f"""
Combine the following C# code chunks from VB6 file '{filename}' into cohesive service files.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Ensure:
1. No duplicate method names
2. Proper class structure with static methods
3. Consistent naming and formatting
4. All necessary using statements (e.g., System.Runtime.InteropServices for J2534)
5. Proper J2534 API integration with [DllImport] and structs

Chunks:
{chr(10).join([f"--- Chunk {i+1} ---{chr(10)}{json.dumps(chunk, indent=2)}" for i, chunk in enumerate(chunks)])}

Return JSON structure:
{{
  "Constants.cs": "C# code for constants class",
  "ModuleService.cs": "C# code for service class",
  "IModuleService.cs": "C# code for service interface"
}}
"""
        return self.call_azure_openai(combine_prompt, max_tokens=16000)

    def combine_class_chunks(self, chunks: List[Dict[str, Any]], filename: str, class_name: str, namespace: str) -> Dict[str, Any]:
        logger.info(f"Combining {len(chunks)} class chunks for {filename}")

        combine_prompt = f"""
Combine the following C# code chunks from VB6 class file '{filename}' (class name: {class_name}) into a cohesive class.
IMPORTANT: Return ONLY a valid JSON object. No markdown, no ```json, no comments, no explanations outside the JSON.
Use namespace: {namespace}
Ensure:
1. Single class definition with proper structure
2. No duplicate methods/properties
3. Proper inheritance and interfaces if needed
4. All necessary using statements (e.g., System.Runtime.InteropServices for J2534)
5. Implement IDisposable for resource cleanup
6. Proper J2534 API integration with [DllImport], [StructLayout], and [MarshalAs]
7. Correct handling of structs like RX_structure, vciSCONFIG, VTX_RT_VERSION_ITEM
8. Memory management for P/Invoke (e.g., Marshal.AllocHGlobal, Marshal.FreeHGlobal)

Class Chunks:
{chr(10).join([f"--- Chunk {i+1} ---{chr(10)}{json.dumps(chunk, indent=2)}" for i, chunk in enumerate(chunks)])}

Return JSON structure:
{{
  "Class.cs": "C# code for the complete converted class"
}}
"""
        return self.call_azure_openai(combine_prompt, max_tokens=16000)

    def convert_cls_file(self, content: str, filename: str, namespace: str) -> Dict[str, Any]:
        logger.info(f"Converting CLS file: {filename}")

        if not content or not content.strip():
            return {"error": f"Empty content in {filename}"}

        class_name = self.extract_class_name(content)
        logger.debug(f"Detected class name: {class_name}")

        purpose = self.classify_cls_purpose(content)
        logger.debug(f"Classified {filename} as {purpose}")

        if len(content) > 12000:
            logger.debug("Class file is large, processing in chunks")
            chunks = self.chunk_large_file(content, max_chunk_size=4000, file_type="cls")
            parts, prev_ctx = [], ""

            for i, chunk in enumerate(chunks):
                logger.debug(f"Processing class chunk {i+1}/{len(chunks)}")
                prompt = self.conversion_prompts['class_chunk_converter'].format(
                    chunk_number=i + 1,
                    total_chunks=len(chunks),
                    previous_context=prev_ctx,
                    vb6_code=chunk,
                    namespace=namespace,
                    class_name=class_name
                )
                result = self.call_azure_openai(prompt, max_tokens=8000)

                if "error" in result:
                    logger.warning(f"Class chunk {i+1} failed: {result['error']}")
                    continue

                parts.append(result)
                prev_ctx = result.get("ContextSummary", "")[:500]

            if not parts:
                return {"error": f"All chunks failed for {filename}"}

            return self.combine_class_chunks(parts, filename, class_name, namespace)
        else:
            prompt = self.conversion_prompts['class_cls'].format(
                vb6_code=content,
                namespace=namespace
            )
            return self.call_azure_openai(prompt)

    def create_csproj_file(self, project_name: str) -> str:
        logger.debug(f"Creating csproj file for {project_name}")
        return f"""<Project Sdk="Microsoft.NET.Sdk.Worker">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <UserSecretsId>dotnet-{project_name}-{datetime.now():%Y%m%d-%H%M%S}</UserSecretsId>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.Extensions.Hosting" Version="9.0.0" />
    <PackageReference Include="Microsoft.Extensions.Logging" Version="9.0.0" />
    <PackageReference Include="Microsoft.Extensions.Configuration" Version="9.0.0" />
    <PackageReference Include="Microsoft.Extensions.Configuration.Json" Version="9.0.0" />
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="System.Data.SqlClient" Version="4.8.5" />
    <PackageReference Include="Serilog.Extensions.Hosting" Version="8.0.0" />
    <PackageReference Include="Serilog.Sinks.File" Version="5.0.0" />
    <PackageReference Include="Serilog.Sinks.Console" Version="5.0.0" />
  </ItemGroup>
</Project>"""

    def create_program_cs(self, project_name: str, namespace: str) -> str:
        logger.debug(f"Creating Program.cs for {project_name}")
        return f"""using {namespace};
using {namespace}.Services;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Serilog;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddHostedService<Worker>();
builder.Services.AddScoped<IModuleService, ModuleService>();
builder.Services.AddSingleton<clsDEM900>();
builder.Services.AddLogging(logging => 
{{
    logging.AddSerilog(new LoggerConfiguration()
        .MinimumLevel.Information()
        .WriteTo.Console()
        .WriteTo.File("logs/worker_{{Date}}.log", 
            rollingInterval: RollingInterval.Day,
            retainedFileCountLimit: 7)
        .CreateLogger());
}});

var host = builder.Build();
host.Run();"""

    def create_worker_cs(self, project_name: str, namespace: str) -> str:
        logger.debug(f"Creating Worker.cs for {project_name}")
        return f"""using {namespace}.Models;
using {namespace}.Services;
using Microsoft.Extensions.Logging;

namespace {namespace};

public class Worker : BackgroundService
{{
    private readonly ILogger<Worker> _logger;
    private readonly IModuleService _moduleService;
    private readonly clsDEM900 _dem900;

    public Worker(ILogger<Worker> logger, IModuleService moduleService, clsDEM900 dem900)
    {{
        _logger = logger;
        _moduleService = moduleService;
        _dem900 = dem900;
    }}

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {{
        while (!stoppingToken.IsCancellationRequested)
        {{
            try
            {{
                _logger.LogInformation("Worker running at: {{time}}", DateTimeOffset.Now);
                await _moduleService.ExecuteMainLogicAsync();
                // Example: Initialize DEM900 communication
                _dem900.Get_DEM900_Info();
                await Task.Delay(1000, stoppingToken);
            }}
            catch (Exception ex)
            {{
                _logger.LogError(ex, "Error occurred executing the service");
                await Task.Delay(5000, stoppingToken);
            }}
        }}
    }}
}}"""

    def create_appsettings_json(self) -> str:
        logger.debug("Creating appsettings.json")
        return json.dumps({
            "Logging": {
                "LogLevel": {
                    "Default": "Information",
                    "Microsoft.Hosting.Lifetime": "Information"
                }
            },
            "Serilog": {
                "MinimumLevel": {
                    "Default": "Information",
                    "Override": {
                        "Microsoft": "Warning",
                        "System": "Warning"
                    }
                },
                "WriteTo": [
                    {
                        "Name": "Console"
                    },
                    {
                        "Name": "File",
                        "Args": {
                            "path": "logs/worker_.log",
                            "rollingInterval": "Day",
                            "retainedFileCountLimit": 7
                        }
                    }
                ]
            },
            "DEM900": {
                "SerialNumber": "DEM900_NONE",
                "SoftwareLocation": "C:\\Path\\To\\DEM900Software"
            }
        }, indent=2)

app = FastAPI(title="VB6 → .NET 9 Worker Converter", version="2.1.2")
converter = VB6Converter()

@app.get("/")
def root():
    logger.info("Root endpoint accessed")
    return {
        "message": "VB6 to .NET 9 Worker Service Converter",
        "version": "2.1.2",
        "features": ["Large file chunking", "Enhanced CLS support", "J2534 API integration", "Dynamic CLS classification"],
        "endpoints": {
            "/convert": "POST - Upload VB6 ZIP for conversion",
            "/health": "GET - Health check"
        }
    }

@app.get("/health")
def health():
    logger.info("Health check endpoint accessed")
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}



@app.post("/convert")
async def convert_vb6_project(file: UploadFile = File(...), namespace: str = "ConvertedApp"):
    logger.info(f"Starting conversion for file: {file.filename} with namespace: {namespace}")

    if not file.filename or not file.filename.endswith(".zip"):
        logger.error("Invalid file type uploaded, expected ZIP")
        raise HTTPException(status_code=400, detail="Please upload a ZIP file")

    if not namespace.replace(".", "").replace("_", "").isalnum():
        logger.error("Invalid namespace provided")
        raise HTTPException(status_code=400, detail="Namespace must be alphanumeric with optional dots and underscores")

    try:
        temp_dir = tempfile.mkdtemp()
        input_dir = Path(temp_dir) / "input"
        output_dir = Path(temp_dir) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        logger.debug(f"Created temporary directories: {temp_dir}")

        zip_path = Path(temp_dir) / file.filename
        with open(zip_path, "wb") as f:
            content = await file.read()
            if len(content) == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")
            f.write(content)
        logger.debug(f"Saved uploaded ZIP file to {zip_path}")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(input_dir)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file")
        logger.debug(f"Extracted ZIP contents to {input_dir}")

        project_name = Path(file.filename).stem
        if not project_name.replace("_", "").replace("-", "").isalnum():
            project_name = "MyWorkerService"
        logger.info(f"Using project name: {project_name}")

        project_root = output_dir / project_name
        for sub in ["Models", "Services", "Helpers", "wwwroot"]:
            (project_root / sub).mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created project directory structure at {project_root}")

        successful_files: List[str] = []
        failed_files: List[str] = []
        large_files: List[str] = []

        for vb_path in input_dir.rglob("*"):
            if not vb_path.is_file():
                continue

            ext = vb_path.suffix.lower()
            if ext not in [".bas", ".cls"]:
                continue

            try:
                content = vb_path.read_text(encoding="utf-8", errors="ignore")
                logger.debug(f"Read file: {vb_path.name} ({len(content)} chars, {len(content.splitlines())} lines)")

                if len(content.strip()) == 0:
                    logger.warning(f"Skipping empty file: {vb_path.name}")
                    failed_files.append(f"{vb_path.name} (empty)")
                    continue

                if len(content) > 10000:
                    large_files.append(f"{vb_path.name} ({len(content.splitlines())} lines)")

            except Exception as e:
                logger.error(f"Error reading {vb_path.name}: {e}")
                failed_files.append(f"{vb_path.name} (read error)")
                continue

            base = vb_path.stem

            if ext == ".bas":
                logger.info(f"Processing BAS file: {vb_path.name}")
                converted = converter.convert_bas_file(content, vb_path.name, namespace)

                if "error" in converted:
                    logger.warning(f"BAS conversion failed for {vb_path.name}: {converted['error']}")
                    failed_files.append(f"{vb_path.name} (conversion failed)")
                    continue

                for file_name, code in converted.items():
                    if file_name.endswith(".cs") and code:
                        sanitized_code = converter.sanitize_code(str(code))
                        if sanitized_code:
                            output_path = project_root / "Services" / file_name
                            output_path.write_text(sanitized_code, encoding="utf-8")
                            logger.debug(f"Wrote {file_name} to Services")

                successful_files.append(vb_path.name)

            elif ext == ".cls":
                logger.info(f"Processing CLS file: {vb_path.name}")
                purpose = converter.classify_cls_purpose(content)
                converted = converter.convert_cls_file(content, vb_path.name, namespace)

                if "error" in converted:
                    logger.warning(f"CLS conversion failed for {vb_path.name}: {converted['error']}")
                    failed_files.append(f"{vb_path.name} (conversion failed)")
                    continue

                for file_name, code in converted.items():
                    if file_name.endswith(".cs") and code:
                        sanitized_code = converter.sanitize_code(str(code))
                        if sanitized_code:
                            target_dir = "Models" if purpose == "model" else "Services"
                            output_path = project_root / target_dir / f"{base}.cs"
                            output_path.write_text(sanitized_code, encoding="utf-8")
                            logger.debug(f"Wrote {base}.cs to {target_dir}")

                successful_files.append(vb_path.name)
                logger.info(f"Classified and saved {vb_path.name} as {purpose}")

        # Boilerplate files
        (project_root / f"{project_name}.csproj").write_text(converter.create_csproj_file(project_name))
        (project_root / "Program.cs").write_text(converter.create_program_cs(project_name, namespace))
        (project_root / "Worker.cs").write_text(converter.create_worker_cs(project_name, namespace))
        (project_root / "appsettings.json").write_text(converter.create_appsettings_json())
        (project_root / "Helpers" / "Constants.cs").write_text(
            f"""namespace {namespace}.Helpers;

public static class Constants
{{
    public const string APPLICATION_NAME = "{project_name}";
    public const string VERSION = "1.0.0";
    public static readonly DateTime BUILD_DATE = DateTime.Parse("{datetime.now().isoformat()}");
}}""")

        readme_content = f"""# {project_name} - Converted from VB6

## Conversion Summary
- **Total files processed**: {len(successful_files) + len(failed_files)}
- **Successfully converted**: {len(successful_files)}
- **Failed conversions**: {len(failed_files)}
- **Large files processed**: {len(large_files)}

## Large Files Handled
{chr(10).join([f"- {file}" for file in large_files]) if large_files else "None"}

## Failed Files
{chr(10).join([f"- {file}" for file in failed_files]) if failed_files else "None"}

## Notes
This project was automatically converted from VB6 to C# .NET 9, with support for J2534 API integration.
Large files were processed in chunks and reassembled.
CLS files were classified as 'model' or 'service' based on content:
- Models: Placed in Models directory (mostly properties).
- Services: Placed in Services directory (J2534 API calls or multiple methods).
Review J2534 DLL imports (BVTX4J32.dll, BVTX-VCI-RT-J.dll) and test with a DEM900 device.
Manual review and testing is recommended.

## Running the Service
```bash
dotnet restore
dotnet build
dotnet run
```

## Dependencies
- .NET 9.0
- Microsoft.Extensions.Hosting
- Serilog for logging
"""
        (project_root / "README.md").write_text(readme_content, encoding="utf-8")
        logger.debug("Generated boilerplate files and README")
        
        output_zip = Path(temp_dir) / f"{project_name}_converted.zip"
        try:
            with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(project_root):
                    for file in files:
                        file_path = Path(root) / file
                        arc_name = file_path.relative_to(output_dir)
                        zf.write(file_path, arcname=arc_name)
                        logger.debug(f"Added {arc_name} to output ZIP")
        except Exception as e:
            logger.error(f"Error creating output ZIP: {e}")
            raise HTTPException(status_code=500, detail=f"Error creating output archive: {e}")

        logger.info(f"Created output ZIP: {output_zip}")

        response_data = {
            "status": "completed",
            "project_name": project_name,
            "successful_files": successful_files,
            "failed_files": failed_files,
            "large_files_processed": large_files,
            "total_files_processed": len(successful_files) + len(failed_files),
            "conversion_summary": {
                "total_files": len(successful_files) + len(failed_files),
                "successful": len(successful_files),
                "failed": len(failed_files),
                "large_files": len(large_files)
            }
        }

        if failed_files:
            response_data["warning"] = f"Some files failed to convert: {', '.join(failed_files[:3])}{'...' if len(failed_files) > 3 else ''}"
        if large_files:
            response_data["info"] = f"Large files were chunked and processed: {len(large_files)} files"

        return FileResponse(
            path=str(output_zip),
            filename=f"{project_name}_converted.zip",
            media_type="application/zip",
            headers={"X-Conversion-Status": json.dumps(response_data)}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")
if __name__ == "__main__":
    logger.info("Starting FastAPI application")
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)