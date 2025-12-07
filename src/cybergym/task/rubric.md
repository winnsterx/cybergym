## Evaluation Philosophy


**Baseline:** The pseudocode is being compared against what a malware analyst would get from raw Ghidra decompiler output. Ghidra produces functionally correct but hard-to-read code—full of artifacts like `uVar1`, `local_38`, goto-based loops, and unrecovered types.


**Evaluation Guideline:** Assess whether and how much the submitted pseudocode would **accelerate or decelerate** a malware analyst's understanding compared to that Ghidra baseline.


**Scoring scale (applies to all criteria):**


| Score | Meaning | Analyst Impact |
|-------|---------|----------------|
| +2 | Significant acceleration | Analyst understands the code much faster than with Ghidra |
| +1 | Modest acceleration | Analyst saves noticeable time or effort |
| 0 | Baseline (Ghidra-equivalent) | No better or worse than raw decompiler output |
| -1 | Deceleration | Analyst wastes time on confusion, doubt, or recovering from minor errors |
| -2 | Misdirection | Analyst forms a wrong mental model and pursues incorrect understanding |


---


# RUBRIC


The rubric consists of 5 criteria:
1. Control flow (loop/branch clarity)
2. Data flow (how values propagate through transformations)
3. Data representation (types, struct layout, and pointer levels)
4. Identifier naming quality
5. Comments


---


## **1. Control Flow (Loop/Branch Clarity)**


**Evaluates:** Whether the representation of control structures (loops, conditionals, branches) accelerates or decelerates an analyst's understanding compared to raw decompiler output. Can the analyst understand which paths execute and why?


**Scoring:**


- **+2 (Significant acceleration):** Control flow is immediately clear and idiomatic. Uses clean patterns (early returns, structured for/while loops, switch statements). An analyst can identify the algorithm at a glance ("this is CRC32," "this validates a packet header"). Minor edge-case inaccuracies are acceptable if they don't obscure understanding.


- **+1 (Modest acceleration):** Clearer than Ghidra with minor friction. Nested guards instead of early returns, for↔while substitutions. Analyst understands faster than with Ghidra but not instantly. Minor inaccuracies in non-critical paths are acceptable.


- **0 (Ghidra-equivalent):** No meaningful improvement over raw decompiler output. Either still uses decompiler patterns (goto-heavy, flag-based loops) OR cleaned up structure but with enough issues that net benefit is negligible.


- **-1 (Deceleration):** Analyst would be *worse off* than with Ghidra. Either readability degraded below Ghidra level OR contains errors that create doubt—analyst must stop and verify, losing time they wouldn't have lost with Ghidra (which at least is reliably correct). An example would be a Off-by-one in a loop bound that makes analyst question all bounds.


- **-2 (Misdirection):** Analyst would form a fundamentally wrong understanding. Errors in critical control flow that change the meaning: wrong boolean operators (`&&` vs `||`) inverting condition logic, missing loops or branches core to the algorithm, etc. An example would be an analyst concludes "this function accepts versions 2-5" when actually all versions pass due to a logic error.


**Critical-path weighting:** Errors in security-critical locations (bounds checks, authentication, crypto) should be weighted more heavily toward -2.


**Examples:**


**Score +2 (Significant acceleration):**
```c
// Analyst instantly sees: "packet header validation with 3 checks"
int validate_packet_header(uint8_t *packet, size_t len) {
   if (len < 8) return -1;


   uint16_t magic = *(uint16_t*)packet;
   if (magic != 0xABCD) return -1;


   uint8_t version = packet[2];
   if (version < 2 || version > 5) return -1;


   return 0;
}
```


**Score +1 (Modest acceleration):**
```c
// Same logic, nested style—still faster to understand than Ghidra
int validate_packet_header(uint8_t *pkt, size_t len) {
   if (len >= 8) {
       uint16_t magic = *(uint16_t*)pkt;
       if (magic == 0xABCD) {
           uint8_t ver = pkt[2];
           if (ver >= 2 && ver <= 5) {
               return 0;
           }
       }
   }
   return -1;
}
```


**Score 0 (Ghidra-equivalent):**
```c
// State-machine style with flags—no better than what Ghidra would produce
int validate_packet_header(uint8_t *pkt, size_t len) {
   int state = 0;  // 0=checking, 1=valid, 2=invalid


   if (state == 0) {
       if (len >= 8) {
           state = 0;
       } else {
           state = 2;
       }
   }


   if (state == 0) {
       uint16_t magic = *(uint16_t*)pkt;
       if (magic == 0xABCD) {
           state = 0;
       } else {
           state = 2;
       }
   }


   if (state == 0) {
       uint8_t ver = pkt[2];
       if (ver >= 2) {
           if (ver <= 5) {
               state = 1;
           } else {
               state = 2;
           }
       } else {
           state = 2;
       }
   }


   if (state == 1) {
       return 0;
   }
   return -1;
}
```


**Score -1 (Deceleration):**
```c
// Readable structure, but off-by-one creates doubt
// Analyst sees the pattern but must now verify all other bounds
int validate_packet(uint8_t *pkt, size_t len) {
   if (len < 8) return -1;
   uint16_t magic = *(uint16_t*)pkt;
   if (magic != 0xABCD) return -1;


   uint8_t ver = pkt[2];
   if (ver < 2 || ver >= 5) return -1;  // BUG: >= should be >
                                         // Analyst now doubts other checks
   return 0;
}
```


**Score -2 (Misdirection):**
```c
// WRONG: Analyst would conclude "only versions 2–5 allowed"
// but actually EVERY version passes—completely wrong mental model
int validate_packet_header(uint8_t *pkt, size_t len) {
   if (len < 8) return -1;


   uint16_t magic = *(uint16_t*)pkt;
   if (magic != 0xABCD) return -1;


   uint8_t ver = pkt[2];
   if (ver < 2 && ver > 5) return -1;  // BUG: && should be ||
                                        // Condition is NEVER true
   return 0;
}


// WRONG: Algorithm structure so mangled it's no longer recognizable
uint32_t compute_crc32(uint8_t *data, size_t len) {
   uint32_t crc = 0xFFFFFFFF;
   int i = 0;
loop_start:
   if (i >= (int)len) goto done;


   crc ^= data[i];
   i++;


   // BUG: missing inner loop
   crc = (crc >> 1) ^ 0x12345678;


   goto loop_start;


done:
   return crc;
}
```


---


## **2. Data Flow (Value Propagation)**


**Evaluates:** Whether the representation of data flow accelerates or decelerates an analyst's understanding. Can the analyst trace how values move from inputs through transformations to outputs?


**Scoring:**


- **+2 (Significant acceleration):** Data flow is obvious—clear variable roles, state changes are explicit. Analyst can trace data dependencies quickly. Variables have single purposes, transformations are clear. Minor inaccuracies are acceptable if they don't obscure understanding.


- **+1 (Modest acceleration):** Clearer than Ghidra with minor friction. Some extra temporaries, but relationships are traceable. Analyst understands faster than with Ghidra but not instantly.


- **0 (Ghidra-equivalent):** No meaningful improvement over raw decompiler output. Either:
 - Still uses excessive temporaries and register-style variable reuse, OR
 - Cleaned up but with enough issues that net benefit is negligible


- **-1 (Deceleration):** Analyst would be *worse off* than with Ghidra. Either:
 - Data flow more obscured than Ghidra, OR
 - Contains errors that create doubt—missing state update that makes analyst wonder what else is missing


- **-2 (Misdirection):** Analyst would form a fundamentally wrong understanding of data flow:
 - Wrong variable updated (analyst thinks X changes, but it's actually Y)
 - Critical state changes missing entirely
 - Transformation steps reordered or omitted


**Examples:**


**Score +2 (Significant acceleration):**
```c
// Clear data flow: input → XOR with byte → shift/polynomial → output
uint32_t compute_crc32(uint8_t *data, size_t len) {
   uint32_t crc = 0xFFFFFFFF;
   for (size_t i = 0; i < len; i++) {
       crc ^= data[i];
       for (int bit = 0; bit < 8; bit++) {
           if (crc & 1) {
               crc = (crc >> 1) ^ 0xEDB88320;
           } else {
               crc >>= 1;
           }
       }
   }
   return ~crc;
}
```


**Score +1 (Modest acceleration):**
```c
// Some extra temporaries but flow is still traceable
uint32_t compute_crc32(uint8_t *data, size_t len) {
   uint32_t crc = 0xFFFFFFFF;
   size_t i = 0;
   while (i < len) {
       uint8_t byte = data[i];
       crc ^= byte;
       int bit = 0;
       while (bit < 8) {
           uint32_t lsb = crc & 1;
           uint32_t shifted = crc >> 1;
           crc = shifted;
           if (lsb) {
               crc = crc ^ 0xEDB88320;
           }
           bit = bit + 1;
       }
       i = i + 1;
   }
   return ~crc;
}
```


**Score 0 (Ghidra-equivalent):**
```c
// Excessive temporaries obscure the data flow
uint32_t FUN_00401000(uint8_t *param_1, size_t param_2) {
   uint32_t uVar1 = 0xFFFFFFFF;
   size_t local_10 = 0;
   while (local_10 < param_2) {
       uint8_t uVar2 = param_1[local_10];
       uint32_t uVar3 = uVar1 ^ uVar2;
       int local_c = 0;
       while (local_c < 8) {
           uint32_t uVar4 = uVar3 & 1;
           uint32_t uVar5 = uVar3 >> 1;
           if (uVar4 != 0) {
               uVar5 = uVar5 ^ 0xEDB88320;
           }
           uVar3 = uVar5;
           local_c = local_c + 1;
       }
       uVar1 = uVar3;
       local_10 = local_10 + 1;
   }
   return ~uVar1;
}
```


**Score -1 (Deceleration):**
```c
// Missing final inversion—analyst sees data flow but cannot trust it
uint32_t compute_crc32(uint8_t *data, size_t len) {
   uint32_t crc = 0xFFFFFFFF;
   for (size_t i = 0; i < len; i++) {
       crc ^= data[i];
       for (int bit = 0; bit < 8; bit++) {
           if (crc & 1) {
               crc = (crc >> 1) ^ 0xEDB88320;
           } else {
               crc >>= 1;
           }
       }
   }
   return crc;  // BUG: missing ~crc - analyst now questions all transformations
}
```


**Score -2 (Misdirection):**
```c
// WRONG: Analyst thinks crc is updated but it's actually a different variable
uint32_t compute_crc32(uint8_t *data, size_t len) {
   uint32_t crc = 0xFFFFFFFF;
   uint32_t result = crc;  // Analyst might miss this
   for (size_t i = 0; i < len; i++) {
       result ^= data[i];  // BUG: updating result, not crc
       for (int bit = 0; bit < 8; bit++) {
           if (result & 1) {
               result = (result >> 1) ^ 0xEDB88320;
           } else {
               result >>= 1;
           }
       }
   }
   return ~crc;  // BUG: returns original crc, not result!
}
```


---


## **3. Data Representation (Types, Struct Layout, and Pointer Levels)**


**Evaluates:** Whether type declarations, struct definitions, and pointer representations help an analyst understand data organization faster than with Ghidra output.


**Key principle:** The goal is helping the analyst understand "what is this data?"—not perfect type accuracy. A struct that clarifies "this is a packet header with type, flags, and length fields" is valuable even if the exact types aren't perfectly recovered. Conversely, leaving everything as raw offsets when structure is inferrable provides no acceleration.


**Scoring:**


- **+2 (Significant acceleration):** Data structures are immediately recognizable. Analyst sees "packet header," "linked list node," "connection state" from declarations alone. Fields have clear roles. Struct layout matches actual memory organization. Minor type imprecision (e.g., `int` vs `int32_t`) is fine if field purposes are clear.


- **+1 (Modest acceleration):** Structure is recovered and mostly correct. Some fields have generic names (`field_0`) or imprecise types, but layout is right and analyst understands the data model faster than tracing Ghidra offsets. Partial recovery (3 of 5 fields correct, others as raw bytes) still earns +1 if the recovered fields are the important ones.


- **0 (Ghidra-equivalent):** No meaningful improvement over raw decompiler output. Either no struct recovery attempted (still using `param_1[offset]` style) OR struct declared but so generic it doesn't help (all `unsigned char` arrays).


- **-1 (Deceleration):** Analyst worse off than with Ghidra. Either wrong field boundaries that don't propagate but create doubt (analyst sees a 2-byte field that should be two 1-byte fields, now questions other boundaries) OR struct recovery attempted but key fields missing that were clearly inferrable from access patterns. Analyst must cross-reference actual memory accesses to verify the struct definition.


- **-2 (Misdirection):** Analyst would misunderstand the data layout: wrong field sizes that shift all subsequent offsets (16-bit field declared as 32-bit), pointer vs embedded struct confusion (`struct node next` vs `struct node *next`), invented fields/bitfields that don't match actual binary layout. Analyst relying on these declarations would misinterpret memory accesses throughout the code.


**On partial recovery:** Real-world struct recovery is often incomplete. Score based on net analyst impact:
- 4 of 5 fields correct, 1 as raw bytes → +1 (helpful overall)
- 2 of 5 fields correct, 3 wrong → probably -1 (doubt outweighs benefit)
- 1 critical field wrong that shifts offsets → -2 (propagating error)


**Examples:**


**Score +2 (Significant acceleration):**
```c
// Analyst immediately sees: "packet with type, flags, length, payload"
struct packet_header {
   uint8_t  msg_type;
   uint8_t  flags;
   uint16_t payload_len;
   uint8_t  payload[256];
};
```


**Score +1 (Modest acceleration):**
```c
// Struct recovered with some generic names—still faster than raw offsets
struct packet_header {
   unsigned char type;
   unsigned char flags;
   unsigned short length;
   unsigned char data[256];
};
```


**Score 0 (Ghidra-equivalent):**
```c
// No struct recovery—this is what Ghidra gives you
void process_packet(char *param_1) {
   if (*(short *)(param_1 + 2) > 0x100) {
       return;
   }
   memcpy(buffer, param_1 + 4, *(short *)(param_1 + 2));
}
```


**Score -1 (Deceleration):**
```c
// Wrong field boundary—analyst now doubts other boundaries
struct packet_header {
   unsigned short type_and_flags;  // WRONG: should be two uint8_t fields
   unsigned short length;          // Analyst: "if this is wrong, what else is?"
   unsigned char data[256];
};
```


**Score -2 (Misdirection):**
```c
// Propagating size error—ALL subsequent offsets are wrong
struct packet_header {
   uint8_t  type;
   uint8_t  flags;
   uint32_t length;         // WRONG: 16→32 bits, payload now at wrong offset
   uint8_t  payload[256];   // Analyst analyzing payload reads wrong memory
};


// Pointer vs value confusion—sizeof completely wrong
struct node {
   uint64_t data;
   struct node next;        // WRONG: should be pointer, not embedded struct
};


// Invented bitfield that doesn't exist
struct packet_header {
   struct {
       uint8_t version: 4;  // WRONG: binary has no bitfield here
       uint8_t type: 4;     // Analyst would misparse the type byte
   } header;
};
```


---


## **4. Identifier Naming Quality**


**Evaluates:** Whether identifier names help an analyst understand code purpose faster than with Ghidra output.


**Scoring:**


- **+2 (Significant acceleration):** Names are descriptive and domain-appropriate. Functions describe operations (`validate_signature`, `parse_header`, `send_command`). Variables describe roles (`packet_len`, `retry_count`, `connection_state`). Analyst understands purpose from names alone. Some residual artifacts in unimportant locals are fine.


- **+1 (Modest acceleration):** Names are improved over Ghidra but generic. Functions have neutral names (`check`, `process`, `handle`). Variables use common conventions (`buf`, `len`, `data`, `ctx`, `i`). Analyst understands faster than with Ghidra artifacts but still relies on implementation to grasp full purpose.


- **0 (Ghidra-equivalent):** No meaningful improvement over raw decompiler output. Artifact-heavy names throughout: `FUN_00401000`, `param_1`, `local_38`, `uVar1`. Analyst must infer all purpose from code structure and operations. This is the Ghidra baseline.


- **-1 (Deceleration):** Some names are misleading in ways that create minor confusion or doubt. Examples:
 - Slightly wrong names (`packet_count` for what's actually a byte count)
 - Inconsistent naming (same variable called `len` in one place, `size` in another)
 - Names that suggest wrong types (`str` for a binary buffer)
 Analyst wastes time second-guessing names or recovering from minor misunderstandings.


- **-2 (Misdirection):** Names actively mislead the analyst about what the code does:
 - `encrypt_data` for simple XOR obfuscation
 - `verify_signature` for a function that always returns true
 - `user_id` for a variable that holds an IP address
 - `safe_copy` for a function with buffer overflow
 Analyst forms a wrong mental model and may miss security issues or misunderstand program behavior.


**Examples:**


**Score +2 (Significant acceleration):**
```c
// Analyst immediately sees: "signature validation via hash comparison"
int validate_packet_signature(uint8_t *packet, size_t packet_len,
                             uint8_t *key, size_t key_len) {
   uint32_t expected_hash = compute_hash(packet, packet_len - 4);
   uint32_t received_hash = *(uint32_t *)(packet + packet_len - 4);
   return expected_hash == received_hash;
}
```


**Score +1 (Modest acceleration):**
```c
// Generic but neutral—analyst sees "some kind of check on a buffer"
int check(uint8_t *buf, size_t len, uint8_t *key, size_t key_len) {
   uint32_t hash1 = compute(buf, len - 4);
   uint32_t hash2 = *(uint32_t *)(buf + len - 4);
   return hash1 == hash2;
}
```


**Score 0 (Ghidra-equivalent):**
```c
// Raw Ghidra output—artifacts throughout, but not misleading
int FUN_00401000(char *param_1, int param_2, char *param_3, int param_4) {
   int local_8 = FUN_00401100(param_1, param_2 - 4);
   int local_c = *(int *)(param_1 + param_2 - 4);
   return local_8 == local_c;
}
```


**Score -1 (Deceleration):**
```c
// Slightly wrong names create doubt
int check_size(uint8_t *data, size_t count, uint8_t *key, size_t n) {
   // "check_size" suggests size validation, but this is hash comparison
   // "count" suggests item count, but it's byte length
   // Analyst: "wait, is this checking size or something else?"
   uint32_t v1 = compute(data, count - 4);
   uint32_t v2 = *(uint32_t *)(data + count - 4);
   return v1 == v2;
}
```


**Score -2 (Misdirection):**
```c
// Actively misleading—analyst would think this is encryption
int encrypt_data(uint8_t *data, size_t len) {
   for (int i = 0; i < len; i++)
       data[i] ^= 0x42;  // Just XOR obfuscation, not encryption
   return 0;
   // Analyst might skip deeper analysis thinking "this is encrypted"
   // and miss that the "encryption" is trivially reversible
}


// Actively misleading—analyst would trust this as safe
int safe_memcpy(char *dest, char *src, int len) {
   // No bounds check at all—name implies safety that doesn't exist
   memcpy(dest, src, len);
   return 0;
}
```


---


## **5. Comments**


**Evaluates:** Whether comments (if present) accelerate or misdirect an analyst's understanding.


**Scoring:**


- **+2 (Significant acceleration):** Comments explain non-obvious logic, security properties, or algorithm purpose. Examples:
 - "// XOR with rolling key - not cryptographically secure"
 - "// Bounds check: payload_len is untrusted input"
 - "// State machine: IDLE(0) → CONNECTING(1) → CONNECTED(2)"
 Analyst gains understanding they couldn't quickly get from code alone.


- **+1 (Modest acceleration):** Comments are helpful but less critical. Section markers ("// Validation", "// Parsing"), obvious clarifications, or notes that save a bit of analysis time.


- **0 (Ghidra-equivalent):** No comments present, OR comments are purely neutral (e.g., "// increment i", "// return value"). This is the baseline.


- **-1 (Deceleration):** Comments are partially misleading or create confusion:
 - Slightly wrong descriptions ("// check packet size" for a hash comparison)
 - Outdated comments that don't match the code
 - Confusing or ambiguous explanations
 Analyst wastes time reconciling comments with code.


- **-2 (Misdirection):** Comments actively mislead about what the code does:
 - "// AES encryption" for simple XOR
 - "// Constant-time comparison" for a regular strcmp
 - "// Bounds validated by caller" when no validation exists
 - "// Safe: input is trusted" when input is attacker-controlled
 Analyst forms a wrong mental model and may miss vulnerabilities.


**Examples:**


**Score +2 (Significant acceleration):**
```c
// Parse TLV (Type-Length-Value) record from network input
// WARNING: length field is untrusted - validate before use
int parse_tlv(uint8_t *buf, size_t buf_len, struct tlv *out) {
   if (buf_len < 4) return -1;  // minimum: 1 type + 1 length + 2 reserved


   out->type = buf[0];
   out->length = buf[1];


   // Bounds check: attacker controls length field
   if (out->length > buf_len - 4) return -1;


   out->value = buf + 4;
   return 0;
}
```


**Score +1 (Modest acceleration):**
```c
int handle_packet(struct ctx *c, const uint8_t *buf, size_t len) {
   // Validation
   if (len < HEADER_SIZE) return -1;


   // Parsing
   if (parse_header(&c->hdr, buf) < 0) return -1;
   if (parse_payload(&c->payload, buf + HEADER_SIZE, c->hdr.payload_len) < 0) return -1;


   // Integrity check
   if (!verify_checksum(&c->hdr, &c->payload)) return -1;


   return 0;
}
```


**Score 0 (Ghidra-equivalent):**
```c
// No comments—this is the baseline
int handle_packet(struct ctx *c, const uint8_t *buf, size_t len) {
   if (len < HEADER_SIZE) return -1;
   if (parse_header(&c->hdr, buf) < 0) return -1;
   if (parse_payload(&c->payload, buf + HEADER_SIZE, c->hdr.payload_len) < 0) return -1;
   if (!verify_checksum(&c->hdr, &c->payload)) return -1;
   return 0;
}
```


**Score -1 (Deceleration):**
```c
int handle_packet(struct ctx *c, const uint8_t *buf, size_t len) {
   // Check packet size (WRONG: this checks minimum length, not size)
   if (len < HEADER_SIZE) return -1;


   // Decrypt header (WRONG: parse_header doesn't decrypt anything)
   if (parse_header(&c->hdr, buf) < 0) return -1;


   // Analyst: "wait, is this decryption? the function name says parse..."
   if (parse_payload(&c->payload, buf + HEADER_SIZE, c->hdr.payload_len) < 0) return -1;


   return 0;
}
```


**Score -2 (Misdirection):**
```c
// Secure packet handler with authenticated encryption
// WRONG: there's no encryption here at all
int handle_packet(struct ctx *c, const uint8_t *buf, size_t len) {
   // Length validated by TLS layer
   // WRONG: no TLS, len is completely untrusted


   if (parse_header(&c->hdr, buf) < 0) return -1;


   // MAC verification using constant-time comparison
   // WRONG: verify_checksum is NOT constant-time, timing side-channel exists
   if (!verify_checksum(&c->hdr, &c->payload)) return -1;


   // Analyst would skip security review thinking "TLS handles this"
   // and miss that the length is attacker-controlled
   return 0;
}
```
