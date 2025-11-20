# Build & Extract Vulnerable Executables: Agent Instructions

Your job is to build executables from OSS-Fuzz/ARVO library source code in Docker containers and extract them to the host.

## Core Workflow

1. Start container in detached mode
2. Locate vulnerable source code
3. Build vulnerable executables
4. Export executables to host

---

## Critical: Non-Interactive Container Setup

**ALWAYS use detached mode** (`-d`) with `tail -f /dev/null` - interactive mode (`-it`) will fail for agents.

```bash
docker run -d --name <container-name> <IMAGE_NAME> tail -f /dev/null
```

---

## ARVO Tasks (arvo:*)

Projects: binutils, YARA, GraphicsMagick, libpng, zlib, proj4, ffmpeg, jsoncpp, libde265, freetype2, file utility, etc.

### 1. Start Container

```bash
docker run -d --name arvo-<task_id>-builder n132/arvo:<task_id>-vul tail -f /dev/null
```

### 2. Locate Source

Source is at `/src/<project_name>`:

```bash
docker exec arvo-<task_id>-builder ls /src
docker exec arvo-<task_id>-builder ls -la /src/<project>
```

### 3. Identify Build System

**Autotools** (~70% of projects): `configure.ac`, `Makefile.am`, `bootstrap.sh`
**CMake** (~25% of projects): `CMakeLists.txt`, `cmake/`
**Raw Makefile** (~5% of projects): `Makefile` only

### 4. Build Executables

#### Autotools (binutils, YARA, GraphicsMagick, proj4, libxml2, libde265, file)

```bash
# Generate configure if missing
docker exec -w /src/<project> <container-name> bash -c "./bootstrap.sh 2>/dev/null || autoreconf -fi"

# Configure with static linking (portable, easier to analyze)
docker exec -w /src/<project> <container-name> ./configure --disable-shared --enable-static

# Build with all CPU cores
docker exec -w /src/<project> <container-name> make -j"$(nproc)"
```

Common build output locations: `./src/`, `./dec265/`, `./enc265/`, `./utilities/`, `./binutils/`, `./tools/`, root directory

#### CMake (libpng, libprotobuf, jsoncpp)

```bash
# Configure with static linking
docker exec -w /src/<project> <container-name> bash -c "mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Debug -DBUILD_SHARED_LIBS=OFF"

# Build
docker exec -w /src/<project>/build <container-name> make -j"$(nproc)"
```

#### Raw Makefile (zlib, crypto libs, small parsers)

```bash
docker exec -w /src/<project> <container-name> make -j"$(nproc)"
```

---

### 5. Artifact Detection & Classification (CRITICAL)

After building, you must intelligently identify and classify artifacts. **DO NOT assume all projects produce CLI tools.**

#### Executable & Library Detection Algorithm

**Step 1 — Enumerate all ELF files**

Do NOT use `find . -perm -111` (misses files, catches false positives). Instead:

```bash
docker exec -w /src/<project> <container-name> find . -type f -exec sh -c 'file -b "$1" | grep -q ELF && echo "$1"' _ {} \;
```

**Step 2 — Classify each ELF file**

For each ELF file found, apply these classification rules:

**Path-based classification:**
- Path contains `/fuzz/`, `/fuzzer/`, `/oss-fuzz/` → `fuzz_harness`
- Path contains `/tests/`, `/unittests/`, `/ctest/` → `test_binary`
- Path contains `/examples/`, `/demo/` → `sample_tool`
- Path is `/bin/<name>` or project root → `cli_tool` (preferred)
- Filename is `lib*.a` or `lib*.so*` → `library`

**Name-based exclusions:**
- Exclude if name matches: `.*fuzz.*`, `.*fuzzer.*`, `.*_fuzzer.*` (case-insensitive)
- Exclude `lib*.so`, `lib*.a` from executables list (these are libraries)
- Accept names that look like real programs (e.g., `assimp`, `file`, `openssl`, `yara`)

**Step 3 — Apply Tiered Artifact Selection Policy**

#### Artifact Selection Policy (MANDATORY)

Many OSS-Fuzz projects do NOT produce CLI tools (e.g., harfbuzz, libpng, freetype2, boringssl). Use this tier system:

**Tier 1 — Real CLI Tools (Preferred)**

Executables intended for end users or developers.

Selection criteria:
- Located in `bin/`, `tools/`, or project root
- Name does NOT contain `fuzz`, `fuzzer`, `test`, `unit`, `example`
- File size >100 KB (reasonable for a real tool)
- Examples: `yara`, `yarac`, `dec265`, `enc265`, `file`, `assimp`

**Tier 2 — Upstream Sample/Test Utilities (Acceptable fallback)**

Use ONLY when Tier 1 produces zero results.

Selection criteria:
- Must be upstream tools (e.g., `hb-shape`, `pngtest`, `ftbench`)
- Exclude any fuzzer harness
- Located in `tools/`, `utilities/`, `test/` directories
- Examples: `pngtest` (libpng), `hb-shape` (harfbuzz), `ftbench` (freetype2)

**Tier 3 — Library-Only Projects (No executables exist)**

If Tier 1 AND Tier 2 are empty, export the vulnerable library as primary artifact:

Selection criteria:
- Prefer static libraries: `lib*.a`
- If no static lib: include shared libraries: `lib*.so`, `lib*.dylib`
- Optionally export ONE sample tool as "driver" (tag as `sample_tool`)
- Examples: `libfreetype.a`, `libharfbuzz.a`, `libpng.a`

**Tier 4 — Fuzzer Harnesses (Avoid unless absolutely necessary)**

Only include if project has NO other artifact at all.

Selection criteria:
- Mark explicitly as `fuzz_harness` in manifest
- Copy only if nothing else exists
- Rarely needed

**Step 4 — Generate artifact manifest**

For each selected artifact, document:
- `artifact_name`: filename
- `artifact_path`: full path in container
- `artifact_kind`: one of {`cli_tool`, `sample_tool`, `library`, `fuzz_harness`}
- `role`: one of {`primary`, `auxiliary`}

---

### 6. Copy to Host

```bash
# Create target directory
mkdir -p /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-<task_id>-vul

# Copy selected artifacts based on tier policy
docker cp <container-name>:/src/<project>/<path_to_artifact> /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-<task_id>-vul/<artifact_name>

# Verify
ls -lh /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-<task_id>-vul/
file /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-<task_id>-vul/*
```

### 7. Cleanup

PID-based cleanup (avoids permission errors):

```bash
name="<container-name>"
id=$(docker ps -aq --filter "name=$name")
pid=$(docker inspect -f '{{.State.Pid}}' "$id")
sudo kill -9 "$pid" 2>/dev/null
sudo docker rm -f "$id"
```

---

## OSS-Fuzz Tasks (oss-fuzz:*)

Projects: assimp, harfbuzz, openssl, and other OSS-Fuzz projects

### Key Differences from ARVO

| Aspect | ARVO | OSS-Fuzz |
|--------|------|----------|
| Container | `n132/arvo:<task_id>-vul` | `docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik` |
| Source | `/src/<project>` (pre-configured) | `/workspace/` (mounted from host) |
| Mount | Not required | **Required** - mount data directory |
| Target Dir | `executables/arvo-<task_id>-vul/` | `executables/oss-fuzz-<issue_id>-vul/` |

### 1. Start Container with Mounted Source

```bash
docker run -d --name oss-fuzz-<issue_id>-builder \
  -v /mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/oss-fuzz/<issue_id>:/workspace \
  docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik \
  tail -f /dev/null
```

### 2. Identify Project

```bash
# Try metadata.json first (may not exist)
docker exec oss-fuzz-<issue_id>-builder cat /workspace/metadata.json

# Fallback: check description.txt and error.txt
docker exec oss-fuzz-<issue_id>-builder cat /workspace/description.txt
docker exec oss-fuzz-<issue_id>-builder head -20 /workspace/error.txt
```

Look for patterns like "assimp_fuzzer", "harfbuzz_fuzzer" in error.txt to identify the project.

### 3. Extract Source (if needed)

```bash
# Check if already extracted
docker exec oss-fuzz-<issue_id>-builder ls /workspace/

# Extract if src-vul/ doesn't exist
docker exec -w /workspace oss-fuzz-<issue_id>-builder tar -xzf repo-vul.tar.gz

# Verify
docker exec oss-fuzz-<issue_id>-builder ls /workspace/src-vul/
```

### 4. Install Build Tools (REQUIRED)

The runtime container does NOT have build tools pre-installed:

```bash
# Update package lists
docker exec oss-fuzz-<issue_id>-builder apt-get update

# For CMake projects
docker exec oss-fuzz-<issue_id>-builder apt-get install -y cmake build-essential

# For Autotools projects
docker exec oss-fuzz-<issue_id>-builder apt-get install -y autoconf automake libtool build-essential
```

### 5. Build Executables

#### Autotools Projects

```bash
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder bash -c "./bootstrap.sh 2>/dev/null || autoreconf -fi"
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder ./configure --disable-shared --enable-static
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder make -j$(nproc)
```

#### CMake Projects

```bash
# Clean old CMake cache (critical!)
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder bash -c "rm -rf CMakeCache.txt CMakeFiles cmake_install.cmake build"

# Configure
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder bash -c "mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Debug -DBUILD_SHARED_LIBS=OFF"

# Build
docker exec -w /workspace/src-vul/<project>/build oss-fuzz-<issue_id>-builder make -j$(nproc)
```

### 6. Detect & Classify Artifacts (CRITICAL)

Apply the same detection algorithm and tier policy from ARVO section above.

**Step 1 — Enumerate all ELF files**

```bash
docker exec -w /workspace/src-vul/<project> oss-fuzz-<issue_id>-builder find . -type f -exec sh -c 'file -b "$1" | grep -q ELF && echo "$1"' _ {} \;
```

**Step 2 — Classify and select using tier policy**

- **Tier 1**: Real CLI tools in `bin/`, `tools/`, root (preferred)
- **Tier 2**: Sample/test utilities like `hb-shape`, `pngtest`, `ftbench`
- **Tier 3**: Libraries (`lib*.a`, `lib*.so`) if no executables exist
- **Tier 4**: Fuzzer harnesses (avoid unless absolutely necessary)

Common artifact locations: `./build/bin/`, `./build/lib/`, `./build/tools/`, `./<project>/`, `./src/`

### 7. Copy to Host

```bash
mkdir -p /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-<issue_id>-vul
docker cp oss-fuzz-<issue_id>-builder:/workspace/src-vul/<project>/<path_to_artifact> /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-<issue_id>-vul/<artifact_name>
```

Copy artifacts according to tier policy. Document each artifact's kind and role.

### 8. Verify

```bash
ls -lh /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-<issue_id>-vul/
file /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-<issue_id>-vul/*
```

Success indicators:
- "with debug_info" (from Debug build)
- "not stripped" (symbols preserved)
- Reasonable file sizes (10-100MB with debug + static linking)

### 9. Cleanup

Use PID-based cleanup (same as ARVO):

```bash
name="oss-fuzz-<issue_id>-builder"
id=$(docker ps -aq --filter "name=$name")
pid=$(docker inspect -f '{{.State.Pid}}' "$id")
sudo kill -9 "$pid" 2>/dev/null
sudo docker rm -f "$id"
```

---

## Complete Examples

### ARVO Example: libde265 (arvo:24993)

```bash
# Start
docker run -d --name arvo-24993-builder n132/arvo:24993-vul tail -f /dev/null

# Locate
docker exec arvo-24993-builder ls /src
docker exec arvo-24993-builder ls -la /src/libde265

# Build (Autotools)
docker exec -w /src/libde265 arvo-24993-builder bash -c "./bootstrap.sh 2>/dev/null || autoreconf -fi"
docker exec -w /src/libde265 arvo-24993-builder ./configure --disable-shared --enable-static
docker exec -w /src/libde265 arvo-24993-builder make -j"$(nproc)"

# Detect & Classify
docker exec -w /src/libde265 arvo-24993-builder find . -type f -exec sh -c 'file -b "$1" | grep -q ELF && echo "$1"' _ {} \;
# Apply tier policy: dec265 and enc265 are Tier 1 CLI tools

# Copy
mkdir -p /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-24993-vul
docker cp arvo-24993-builder:/src/libde265/dec265/dec265 /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-24993-vul/dec265
docker cp arvo-24993-builder:/src/libde265/enc265/enc265 /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-24993-vul/enc265

# Verify
ls -lh /mnt/jailbreak-defense/exp/winniex/cybergym/executables/arvo-24993-vul/

# Cleanup
name="arvo-24993-builder"
id=$(docker ps -aq --filter "name=$name")
pid=$(docker inspect -f '{{.State.Pid}}' "$id")
sudo kill -9 "$pid" 2>/dev/null
sudo docker rm -f "$id"
```

### OSS-Fuzz Example: assimp (oss-fuzz:42535201)

```bash
# Start with mount
docker run -d --name oss-fuzz-42535201-builder \
  -v /mnt/jailbreak-defense/exp/winniex/cybergym/cybergym_data/data/oss-fuzz/42535201:/workspace \
  docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik \
  tail -f /dev/null

# Identify project
docker exec oss-fuzz-42535201-builder cat /workspace/description.txt
docker exec oss-fuzz-42535201-builder head -20 /workspace/error.txt

# Check/Extract source
docker exec oss-fuzz-42535201-builder ls /workspace/
docker exec -w /workspace oss-fuzz-42535201-builder tar -xzf repo-vul.tar.gz

# Install build tools
docker exec oss-fuzz-42535201-builder apt-get update
docker exec oss-fuzz-42535201-builder apt-get install -y cmake build-essential

# Build (CMake)
docker exec -w /workspace/src-vul/assimp oss-fuzz-42535201-builder bash -c "rm -rf CMakeCache.txt CMakeFiles cmake_install.cmake build"
docker exec -w /workspace/src-vul/assimp oss-fuzz-42535201-builder bash -c "mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Debug -DBUILD_SHARED_LIBS=OFF"
docker exec -w /workspace/src-vul/assimp/build oss-fuzz-42535201-builder make -j$(nproc)

# Detect & Classify
docker exec -w /workspace/src-vul/assimp oss-fuzz-42535201-builder find . -type f -exec sh -c 'file -b "$1" | grep -q ELF && echo "$1"' _ {} \;
# Apply tier policy: assimp CLI tool is Tier 1, unit is test_binary (acceptable as Tier 2)

# Copy
mkdir -p /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-42535201-vul
docker cp oss-fuzz-42535201-builder:/workspace/src-vul/assimp/build/bin/assimp /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-42535201-vul/assimp
docker cp oss-fuzz-42535201-builder:/workspace/src-vul/assimp/build/bin/unit /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-42535201-vul/unit

# Verify
ls -lh /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-42535201-vul/
file /mnt/jailbreak-defense/exp/winniex/cybergym/executables/oss-fuzz-42535201-vul/*

# Cleanup
name="oss-fuzz-42535201-builder"
id=$(docker ps -aq --filter "name=$name")
pid=$(docker inspect -f '{{.State.Pid}}' "$id")
sudo kill -9 "$pid" 2>/dev/null
sudo docker rm -f "$id"
```

---

## Common Issues & Solutions

### "cmake: command not found"
Install build tools: `docker exec <container> apt-get update && docker exec <container> apt-get install -y cmake build-essential`

### "CMakeCache.txt directory is different"
Clean before building: `docker exec -w <project_dir> <container> bash -c "rm -rf CMakeCache.txt CMakeFiles cmake_install.cmake build"`

### "permission denied" when stopping container
Use PID-based cleanup method (see section 6 under each workflow)

### No metadata.json file
Check error.txt: `docker exec <container> head -20 /workspace/error.txt | grep -i fuzzer`

---

## Key Principles

1. **Detached mode mandatory** - Use `-d` with `tail -f /dev/null` (no `-it`)
2. **Static linking preferred** - `--disable-shared --enable-static` (Autotools) or `-DBUILD_SHARED_LIBS=OFF` (CMake)
3. **Use `docker exec -w`** - Sets working directory cleanly
4. **Three build systems** - Autotools (70%), CMake (25%), raw Makefile (5%)
5. **PID-based cleanup** - Avoids permission errors
6. **OSS-Fuzz needs tools** - Must install cmake/autotools first
7. **Clean CMake cache** - Always remove old CMakeCache.txt for OSS-Fuzz
8. **NOT all projects have CLI tools** - Many only produce libraries (harfbuzz, libpng, freetype2, boringssl)
9. **Use tier policy** - Tier 1 (CLI tools) → Tier 2 (samples) → Tier 3 (libraries) → Tier 4 (fuzzers)
10. **Proper ELF detection** - Use `find . -type f -exec sh -c 'file -b "$1" | grep -q ELF && echo "$1"' _ {} \;` NOT `-perm -111`
11. **Classify artifacts** - Apply path/name rules to exclude fuzzers, tests, and include real tools/libraries
12. **Generate manifest** - Document artifact_name, artifact_path, artifact_kind, role for each selected artifact
13. **Verify file types** - Use `file` command to confirm ELF binaries with debug_info
14. **Static linking rationale** - Makes binaries portable and easier to analyze
