"use strict";
// WAT Emitter - generates WebAssembly Text Format from FIL AST
// Uses state machine info from AwaitLifter
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.WatEmitter = void 0;
const AST = __importStar(require("./ast"));
const await_lifter_1 = require("./await_lifter");
class WatEmitter {
    constructor(functions, stateMachines, lifter) {
        this.strings = [];
        this.stringMap = new Map(); // value -> addr
        this.nextStringAddr = 256; // start static pool at 0x100
        this.asyncFuncNames = [];
        // Track which built-ins are actually used
        this.usedBuiltins = new Set();
        /**
         * Map from action identifier → effective protocol id. The protocol id is
         * the action's identifier by default, or the `id` string literal from the
         * action's body when present (lets authors use kebab-case node ids without
         * having to make the FIL identifier itself dashed).
         */
        this.actionIds = new Map();
        this.functions = functions;
        this.stateMachines = stateMachines;
        this.lifter = lifter;
        this.asyncFuncNames = Array.from(stateMachines.keys());
    }
    // ─── String pool ────────────────────────────────────────────────────────────
    internString(value) {
        if (this.stringMap.has(value)) {
            return this.stringMap.get(value);
        }
        const addr = this.nextStringAddr;
        this.strings.push({ value, addr });
        this.stringMap.set(value, addr);
        // 4 bytes length + utf8 bytes
        const utf8Len = Buffer.byteLength(value, 'utf8');
        this.nextStringAddr += 4 + utf8Len;
        // Align to 4
        this.nextStringAddr = (this.nextStringAddr + 3) & ~3;
        return addr;
    }
    emptyStringAddr() {
        return this.internString('');
    }
    // ─── WAT Data Segments ─────────────────────────────────────────────────────
    buildDataSegments() {
        const lines = [];
        for (const entry of this.strings) {
            const utf8 = Buffer.from(entry.value, 'utf8');
            // Length prefix (4 bytes, little-endian)
            const len = utf8.length;
            let dataStr = '';
            // Write length bytes
            dataStr += this.byteToHex(len & 0xff);
            dataStr += this.byteToHex((len >> 8) & 0xff);
            dataStr += this.byteToHex((len >> 16) & 0xff);
            dataStr += this.byteToHex((len >> 24) & 0xff);
            // Write UTF-8 bytes
            for (let i = 0; i < utf8.length; i++) {
                const b = utf8[i];
                if (b >= 0x20 && b < 0x7f && b !== 0x22 && b !== 0x5c) {
                    dataStr += String.fromCharCode(b);
                }
                else {
                    dataStr += this.byteToHex(b);
                }
            }
            lines.push(`  (data (i32.const ${entry.addr}) "${dataStr}")`);
        }
        return lines;
    }
    byteToHex(b) {
        return `\\${b.toString(16).padStart(2, '0')}`;
    }
    emit(program) {
        this.actionIds = new Map();
        for (const a of program.actions) {
            this.actionIds.set(a.name, resolveActionId(a));
        }
        // Pre-scan all strings to build the string pool (starts at 0x100)
        this.preScanStrings(program);
        const out = [];
        out.push('(module');
        // All imports MUST precede any memory/global/data/function definitions (WAT rule)
        // WASI imports
        out.push('  ;; WASI imports (for async protocol)');
        out.push('  (import "wasi_snapshot_preview1" "fd_read"');
        out.push('    (func $wasi_fd_read (param i32 i32 i32 i32) (result i32)))');
        out.push('  (import "wasi_snapshot_preview1" "fd_write"');
        out.push('    (func $wasi_fd_write (param i32 i32 i32 i32) (result i32)))');
        out.push('  (import "wasi_snapshot_preview1" "proc_exit"');
        out.push('    (func $wasi_proc_exit (param i32)))');
        // Remaining fil host imports (datetime, uuid, console)
        out.push('  (import "fil" "now_millis" (func $host_now_millis (result i64)))');
        out.push('  (import "fil" "get_uuid" (func $host_get_uuid (result i32)))');
        out.push('  (import "fil" "console_log" (func $host_console_log (param i32)))');
        out.push('');
        // Memory (after imports)
        out.push('  ;; Memory');
        out.push('  (memory (export "memory") 1)');
        out.push('');
        // Protocol globals
        out.push('  ;; WASI protocol globals');
        out.push('  (global $stdin_pos (mut i32) (i32.const 0))');
        out.push('  (global $stdin_len (mut i32) (i32.const 0))');
        out.push('  (global $out_len   (mut i32) (i32.const 0))');
        out.push('');
        // Heap allocator at fixed address
        out.push('  ;; Heap allocator (fixed start at 0x8000)');
        out.push(`  (global $heap_ptr (mut i32) (i32.const ${WatEmitter.HEAP_START}))`);
        out.push('');
        // JSON parser globals (recursive descent state)
        out.push('  ;; JSON parser state');
        out.push('  (global $_json_data (mut i32) (i32.const 0))');
        out.push('  (global $_json_pos  (mut i32) (i32.const 0))');
        out.push('  (global $_json_len  (mut i32) (i32.const 0))');
        out.push('  (global $_json_tag  (mut i32) (i32.const 0))');
        out.push('');
        // Protocol data segments (raw bytes at fixed addresses in reserved region)
        out.push('  ;; Protocol literal strings (raw bytes, no length prefix)');
        out.push('  ;; stdout literals (full decision format)');
        out.push('  (data (i32.const 0x20) "executeNode:\\0a  name: ")');
        out.push('  (data (i32.const 0x35) "\\0a  input: |\\0a    ")');
        out.push('  (data (i32.const 0x45) "\\0a\\0a")');
        out.push('  ;; stdin literals (compact history format)');
        out.push('  (data (i32.const 0x47) "node: ")');
        out.push('  (data (i32.const 0x4d) "\\0a  output: ")');
        out.push('  ;; shared');
        out.push('  (data (i32.const 0x58) "flowCompleted:\\0a  success: true\\0a")');
        out.push('  ;; Phase 3 — executeTimer');
        out.push('  (data (i32.const 0x78) "executeTimer:\\0a  deadline: ")');
        out.push('  (data (i32.const 0x92) "executeTimer:\\0a  duration: ")');
        out.push('  (data (i32.const 0xac) "timer: ")');
        out.push('  (data (i32.const 0xb3) "\\0a")');
        out.push('  ;; Phase 4-5 — parallel/race markers');
        out.push('  (data (i32.const 0xb4) "all: ")');
        out.push('  (data (i32.const 0xb9) "race: ")');
        out.push('  (data (i32.const 0xbf) " winner=")');
        out.push('  (data (i32.const 0xc7) "nodeCancelled:\\0a")');
        out.push('  (data (i32.const 0xd6) "timerCancelled:\\0a")');
        out.push('  ;; Phase 6 — deterministic host-call records');
        out.push('  (data (i32.const 0xe6) "now: ")');
        out.push('  (data (i32.const 0xeb) "uuid: ")');
        out.push('');
        // Generate all code first so that internString() calls in builtins are captured
        const allocFn = [];
        allocFn.push('  ;; Bump allocator');
        allocFn.push('  (func $alloc (param $size i32) (result i32)');
        allocFn.push('    (local $ptr i32)');
        allocFn.push('    (local.set $ptr (global.get $heap_ptr))');
        allocFn.push('    (global.set $heap_ptr (i32.add (global.get $heap_ptr) (local.get $size)))');
        allocFn.push('    (local.get $ptr)');
        allocFn.push('  )');
        allocFn.push('');
        const protocolLines = this.emitProtocolHelpers();
        const builtinLines = this.emitBuiltins();
        const userFnLines = [];
        for (const fn of program.functions) {
            userFnLines.push(...this.emitSyncFunction(fn, fn.isAsync));
            userFnLines.push('');
        }
        const entryLines = [];
        entryLines.push('  ;; Entry point');
        entryLines.push('  (func $fil_start (export "start")');
        entryLines.push('    (call $init_io)');
        const mainFn = program.functions.find(f => f.name === 'main');
        if (mainFn) {
            // Flow-level main params model trigger inputs for conversion; flow-run
            // does not inject an event payload yet, so local execution starts with
            // zero/default placeholders.
            for (const p of mainFn.params) {
                const defaultValue = this.defaultValueForType(p.type);
                if (defaultValue)
                    entryLines.push(`    ${defaultValue}`);
            }
            entryLines.push('    (call $main)');
            const mainRet = this.watType(mainFn.returnType);
            if (mainRet !== 'void' && mainRet !== '') {
                entryLines.push('    drop');
            }
        }
        entryLines.push(`    (call $out_write_raw (i32.const ${WatEmitter.P_FLOW_DONE}) (i32.const ${WatEmitter.P_FLOW_LEN}))`);
        entryLines.push('    (call $out_flush)');
        entryLines.push('  )');
        entryLines.push('');
        // Now build data segments (all strings including those interned by builtins are captured)
        const dataSegs = this.buildDataSegments();
        if (dataSegs.length > 0) {
            out.push('  ;; Static string data');
            out.push(...dataSegs);
            out.push('');
        }
        // Emit all generated code
        out.push(...allocFn);
        out.push(...protocolLines);
        out.push(...builtinLines);
        out.push(...userFnLines);
        out.push(...entryLines);
        out.push(')');
        return out.join('\n');
    }
    // ─── WASI protocol helper functions ─────────────────────────────────────────
    emitProtocolHelpers() {
        const E = WatEmitter;
        const lines = [];
        lines.push('  ;; === WASI Protocol Helpers ===');
        lines.push('');
        // $init_io — read all stdin into buffer at startup
        lines.push('  (func $init_io');
        lines.push(`    (i32.store (i32.const ${E.SCRATCH_ADDR})   (i32.const ${E.STDIN_BUF}))`);
        lines.push(`    (i32.store (i32.const ${E.SCRATCH_ADDR + 4}) (i32.const ${E.STDIN_SIZE}))`);
        lines.push(`    (drop (call $wasi_fd_read (i32.const 0) (i32.const ${E.SCRATCH_ADDR}) (i32.const 1) (i32.const ${E.NREAD_ADDR})))`);
        lines.push(`    (global.set $stdin_len (i32.load (i32.const ${E.NREAD_ADDR})))`);
        lines.push('  )');
        lines.push('');
        // $out_write_raw — append raw bytes to output buffer
        lines.push('  (func $out_write_raw (param $src i32) (param $len i32)');
        lines.push('    (memory.copy');
        lines.push(`      (i32.add (i32.const ${E.OUT_BUF}) (global.get $out_len))`);
        lines.push('      (local.get $src)');
        lines.push('      (local.get $len))');
        lines.push('    (global.set $out_len (i32.add (global.get $out_len) (local.get $len)))');
        lines.push('  )');
        lines.push('');
        // $out_write_str — append FIL string content (skipping 4-byte length prefix)
        lines.push('  (func $out_write_str (param $str i32)');
        lines.push('    (local $len i32)');
        lines.push('    (local.set $len (i32.load (local.get $str)))');
        lines.push('    (call $out_write_raw (i32.add (local.get $str) (i32.const 4)) (local.get $len))');
        lines.push('  )');
        lines.push('');
        // $out_flush — write output buffer to stdout
        lines.push('  (func $out_flush');
        lines.push(`    (i32.store (i32.const ${E.SCRATCH_ADDR})   (i32.const ${E.OUT_BUF}))`);
        lines.push(`    (i32.store (i32.const ${E.SCRATCH_ADDR + 4}) (global.get $out_len))`);
        lines.push(`    (drop (call $wasi_fd_write (i32.const 1) (i32.const ${E.SCRATCH_ADDR}) (i32.const 1) (i32.const ${E.NREAD_ADDR})))`);
        lines.push('    (global.set $out_len (i32.const 0))');
        lines.push('  )');
        lines.push('');
        // $stdin_check_bytes — check if stdin at pos matches bytes; advance pos if match
        // returns 1 on match, 0 otherwise
        lines.push('  (func $stdin_check_bytes (param $ptr i32) (param $len i32) (result i32)');
        lines.push('    (local $i i32)');
        lines.push('    (local $pos i32)');
        lines.push('    (local.set $pos (global.get $stdin_pos))');
        lines.push('    (if (i32.gt_u (i32.add (local.get $pos) (local.get $len)) (global.get $stdin_len))');
        lines.push('      (then (return (i32.const 0))))');
        lines.push('    (local.set $i (i32.const 0))');
        lines.push('    (block $done');
        lines.push('      (loop $lp');
        lines.push('        (br_if $done (i32.ge_u (local.get $i) (local.get $len)))');
        lines.push('        (if (i32.ne');
        lines.push(`          (i32.load8_u (i32.add (i32.const ${E.STDIN_BUF}) (i32.add (local.get $pos) (local.get $i))))`);
        lines.push('          (i32.load8_u (i32.add (local.get $ptr) (local.get $i))))');
        lines.push('          (then (return (i32.const 0))))');
        lines.push('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        lines.push('        (br $lp)');
        lines.push('      )');
        lines.push('    )');
        lines.push('    (global.set $stdin_pos (i32.add (local.get $pos) (local.get $len)))');
        lines.push('    (i32.const 1)');
        lines.push('  )');
        lines.push('');
        // $stdin_str_match — check if stdin at pos matches FIL string content; advance if match
        lines.push('  (func $stdin_str_match (param $str i32) (result i32)');
        lines.push('    (call $stdin_check_bytes');
        lines.push('      (i32.add (local.get $str) (i32.const 4))');
        lines.push('      (i32.load (local.get $str)))');
        lines.push('  )');
        lines.push('');
        // $stdin_read_until_newline — read bytes from stdin until \n into a heap FIL string
        lines.push('  (func $stdin_read_until_newline (result i32)');
        lines.push('    (local $result i32) (local $len i32) (local $c i32)');
        lines.push('    (local.set $result (call $alloc (i32.const 4096)))');
        lines.push('    (local.set $len (i32.const 0))');
        lines.push('    (block $done');
        lines.push('      (loop $lp');
        lines.push('        (br_if $done (i32.ge_u (global.get $stdin_pos) (global.get $stdin_len)))');
        lines.push(`        (local.set $c (i32.load8_u (i32.add (i32.const ${E.STDIN_BUF}) (global.get $stdin_pos))))`);
        lines.push('        (global.set $stdin_pos (i32.add (global.get $stdin_pos) (i32.const 1)))');
        lines.push('        (br_if $done (i32.eq (local.get $c) (i32.const 10)))');
        lines.push('        (i32.store8 (i32.add (i32.add (local.get $result) (i32.const 4)) (local.get $len)) (local.get $c))');
        lines.push('        (local.set $len (i32.add (local.get $len) (i32.const 1)))');
        lines.push('        (br $lp)');
        lines.push('      )');
        lines.push('    )');
        lines.push('    (i32.store (local.get $result) (local.get $len))');
        lines.push('    (local.get $result)');
        lines.push('  )');
        lines.push('');
        // $protocol_execute_node — core decision protocol
        // Stdout: writes full executeNode decision (with input) for the host
        // Stdin: reads compact history format (node: name\n  output: value\n)
        lines.push('  (func $protocol_execute_node (param $name i32) (param $input i32) (result i32)');
        // Write full decision to stdout
        lines.push(`    (call $out_write_raw (i32.const ${E.P_EXEC_NODE}) (i32.const ${E.P_EXEC_LEN}))`);
        lines.push('    (call $out_write_str (local.get $name))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_INPUT}) (i32.const ${E.P_INPUT_LEN}))`);
        lines.push('    (call $out_write_str (local.get $input))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        // Match compact history entry in stdin: "node: <name>\n  output: <value>\n"
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_NODE}) (i32.const ${E.P_NODE_LEN})))`);
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push('    (if (i32.eqz (call $stdin_str_match (local.get $name)))');
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        // After name match, stdin_pos is right after the name bytes (next byte is '\n').
        // P_OUTPUT begins with '\n', so we do NOT skip the line here.
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_OUTPUT}) (i32.const ${E.P_OUTPUT_LEN})))`);
        // Read inline output value until newline
        lines.push('    (call $stdin_read_until_newline)');
        lines.push('  )');
        lines.push('');
        // $protocol_execute_timer_dt — absolute deadline form
        // Stdout: "executeTimer:\n  deadline: <ISO>\n\n"
        // Stdin: "timer: <ISO>\n" → parse and return as i64
        lines.push('  (func $protocol_execute_timer_dt (param $deadline i64) (result i64)');
        lines.push('    (local $iso i32)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_TIMER_DL}) (i32.const ${E.P_TIMER_DL_LEN}))`);
        lines.push('    (local.set $iso (call $datetime_to_iso (local.get $deadline)))');
        lines.push('    (call $out_write_str (local.get $iso))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_TIMER}) (i32.const ${E.P_TIMER_LEN})))`);
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push('    (call $datetime_parse_iso (call $stdin_read_until_newline))');
        lines.push('  )');
        lines.push('');
        // $protocol_execute_timer_ts — relative duration form
        // Stdout: "executeTimer:\n  duration: <ms>\n\n"
        // Stdin: "timer: <ISO>\n" → parse and return as i64
        lines.push('  (func $protocol_execute_timer_ts (param $duration i64) (result i64)');
        lines.push('    (local $ms_str i32)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_TIMER_DUR}) (i32.const ${E.P_TIMER_DUR_LEN}))`);
        lines.push('    (local.set $ms_str (call $i64_to_str (local.get $duration)))');
        lines.push('    (call $out_write_str (local.get $ms_str))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_TIMER}) (i32.const ${E.P_TIMER_LEN})))`);
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push('    (call $datetime_parse_iso (call $stdin_read_until_newline))');
        lines.push('  )');
        lines.push('');
        // ── Phase 4-5: per-decision write/read helpers (no proc_exit on stdin miss) ──
        // These split the protocol into a "write decision" half and a "read history"
        // half so that Promise.all/any can write all entries first and then match
        // history positionally.
        lines.push('  (func $write_node_decision (param $name i32) (param $input i32)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_EXEC_NODE}) (i32.const ${E.P_EXEC_LEN}))`);
        lines.push('    (call $out_write_str (local.get $name))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_INPUT}) (i32.const ${E.P_INPUT_LEN}))`);
        lines.push('    (call $out_write_str (local.get $input))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        lines.push('  )');
        lines.push('');
        lines.push('  (func $write_timer_dt_decision (param $deadline i64)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_TIMER_DL}) (i32.const ${E.P_TIMER_DL_LEN}))`);
        lines.push('    (call $out_write_str (call $datetime_to_iso (local.get $deadline)))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        lines.push('  )');
        lines.push('');
        lines.push('  (func $write_timer_ts_decision (param $duration i64)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_TIMER_DUR}) (i32.const ${E.P_TIMER_DUR_LEN}))`);
        lines.push('    (call $out_write_str (call $i64_to_str (local.get $duration)))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_END}) (i32.const ${E.P_END_LEN}))`);
        lines.push('  )');
        lines.push('');
        // $read_node_result(name) — matches "node: <name>\n  output: <val>\n", returns val.
        // proc_exits if stdin doesn't match the expected entry.
        lines.push('  (func $read_node_result (param $name i32) (result i32)');
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_NODE}) (i32.const ${E.P_NODE_LEN})))`);
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push('    (if (i32.eqz (call $stdin_str_match (local.get $name)))');
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_OUTPUT}) (i32.const ${E.P_OUTPUT_LEN})))`);
        lines.push('    (call $stdin_read_until_newline)');
        lines.push('  )');
        lines.push('');
        // $read_timer_result — matches "timer: <ISO>\n", returns the parsed epoch ms.
        lines.push('  (func $read_timer_result (result i64)');
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_TIMER}) (i32.const ${E.P_TIMER_LEN})))`);
        lines.push('      (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        lines.push('    (call $datetime_parse_iso (call $stdin_read_until_newline))');
        lines.push('  )');
        lines.push('');
        // $write_all_marker — writes "all: N\n" to stdout.
        lines.push('  (func $write_all_marker (param $n i32)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_ALL}) (i32.const ${E.P_ALL_LEN}))`);
        lines.push('    (call $out_write_str (call $i32_to_str (local.get $n)))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN}))`);
        lines.push('  )');
        lines.push('');
        // $match_all_marker — returns 1 if stdin has "all: N\n" at the cursor (and
        // advances past it), 0 otherwise (cursor unchanged on miss).
        lines.push('  (func $match_all_marker (param $n i32) (result i32)');
        lines.push('    (local $save i32)');
        lines.push('    (local.set $save (global.get $stdin_pos))');
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_ALL}) (i32.const ${E.P_ALL_LEN})))`);
        lines.push('      (then (global.set $stdin_pos (local.get $save)) (return (i32.const 0))))');
        lines.push('    (if (i32.eqz (call $stdin_str_match (call $i32_to_str (local.get $n))))');
        lines.push('      (then (global.set $stdin_pos (local.get $save)) (return (i32.const 0))))');
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN})))`);
        lines.push('    (i32.const 1)');
        lines.push('  )');
        lines.push('');
        // $write_race_marker — writes "race: N\n" to stdout.
        lines.push('  (func $write_race_marker (param $n i32)');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_RACE}) (i32.const ${E.P_RACE_LEN}))`);
        lines.push('    (call $out_write_str (call $i32_to_str (local.get $n)))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN}))`);
        lines.push('  )');
        lines.push('');
        // $match_race_marker — examines stdin for the race marker.
        //   returns >= 0 → the K-th entry won; cursor advanced past "race: N winner=K\n"
        //   returns -1   → stdin has no race marker for this group (first replay)
        //   returns -2   → stdin has "race: N\n" but no winner=K (race in flight)
        // On both -1 and -2 the cursor is restored.
        lines.push('  (func $match_race_marker (param $n i32) (result i32)');
        lines.push('    (local $save i32) (local $k i32) (local $c i32)');
        lines.push('    (local.set $save (global.get $stdin_pos))');
        lines.push(`    (if (i32.eqz (call $stdin_check_bytes (i32.const ${E.P_RACE}) (i32.const ${E.P_RACE_LEN})))`);
        lines.push('      (then (global.set $stdin_pos (local.get $save)) (return (i32.const -1))))');
        lines.push('    (if (i32.eqz (call $stdin_str_match (call $i32_to_str (local.get $n))))');
        lines.push('      (then (global.set $stdin_pos (local.get $save)) (return (i32.const -1))))');
        lines.push(`    (if (call $stdin_check_bytes (i32.const ${E.P_WINNER}) (i32.const ${E.P_WINNER_LEN}))`);
        lines.push('      (then');
        lines.push('        (local.set $k (i32.const 0))');
        lines.push('        (block $kdone (loop $klp');
        lines.push('          (br_if $kdone (i32.ge_u (global.get $stdin_pos) (global.get $stdin_len)))');
        lines.push(`          (local.set $c (i32.load8_u (i32.add (i32.const ${WatEmitter.STDIN_BUF}) (global.get $stdin_pos))))`);
        lines.push('          (br_if $kdone (i32.lt_u (local.get $c) (i32.const 48)))');
        lines.push('          (br_if $kdone (i32.gt_u (local.get $c) (i32.const 57)))');
        lines.push('          (local.set $k (i32.add (i32.mul (local.get $k) (i32.const 10))');
        lines.push('                                 (i32.sub (local.get $c) (i32.const 48))))');
        lines.push('          (global.set $stdin_pos (i32.add (global.get $stdin_pos) (i32.const 1)))');
        lines.push('          (br $klp)))');
        lines.push(`        (drop (call $stdin_check_bytes (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN})))`);
        lines.push('        (return (local.get $k))))');
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN})))`);
        lines.push('    (return (i32.const -2))');
        lines.push('  )');
        lines.push('');
        // Cancellation skips for Promise.any losing entries.
        lines.push('  (func $skip_cancelled_node');
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_NODE_CANC}) (i32.const ${E.P_NODE_CANC_LEN})))`);
        lines.push('  )');
        lines.push('');
        lines.push('  (func $skip_cancelled_timer');
        lines.push(`    (drop (call $stdin_check_bytes (i32.const ${E.P_TIMER_CANC}) (i32.const ${E.P_TIMER_CANC_LEN})))`);
        lines.push('  )');
        lines.push('');
        // ── Phase 6: deterministic getDateTime / getUuid ────────────────────
        //
        // Both host calls must be replay-stable: the very first time a program
        // reaches one, we query the host and write the result to stdout so that
        // the host can record it in history. On subsequent replays the recorded
        // value is read from stdin instead of re-querying the host.
        //
        // Unlike executeNode, these helpers do NOT proc_exit on a stdin miss —
        // the value can be obtained inline, so the program continues running
        // and may emit further decisions in the same invocation.
        // Parse a base-10 i64 from stdin starting at the cursor; advances past
        // the digits and (if present) a leading '-'.
        lines.push('  (func $stdin_parse_i64 (result i64)');
        lines.push('    (local $r i64) (local $neg i32) (local $c i32)');
        lines.push(`    (if (i32.lt_u (global.get $stdin_pos) (global.get $stdin_len)) (then`);
        lines.push(`      (if (i32.eq (i32.load8_u (i32.add (i32.const ${WatEmitter.STDIN_BUF}) (global.get $stdin_pos))) (i32.const 45)) (then`);
        lines.push('        (local.set $neg (i32.const 1))');
        lines.push('        (global.set $stdin_pos (i32.add (global.get $stdin_pos) (i32.const 1)))))))');
        lines.push('    (block $done (loop $lp');
        lines.push('      (br_if $done (i32.ge_u (global.get $stdin_pos) (global.get $stdin_len)))');
        lines.push(`      (local.set $c (i32.load8_u (i32.add (i32.const ${WatEmitter.STDIN_BUF}) (global.get $stdin_pos))))`);
        lines.push('      (br_if $done (i32.lt_u (local.get $c) (i32.const 48)))');
        lines.push('      (br_if $done (i32.gt_u (local.get $c) (i32.const 57)))');
        lines.push('      (local.set $r (i64.add (i64.mul (local.get $r) (i64.const 10))');
        lines.push('                             (i64.extend_i32_u (i32.sub (local.get $c) (i32.const 48)))))');
        lines.push('      (global.set $stdin_pos (i32.add (global.get $stdin_pos) (i32.const 1)))');
        lines.push('      (br $lp)))');
        lines.push('    (if (local.get $neg) (then');
        lines.push('      (local.set $r (i64.sub (i64.const 0) (local.get $r)))))');
        lines.push('    (local.get $r)');
        lines.push('  )');
        lines.push('');
        // $protocol_get_datetime — reads "now: <i64>\n" if present in stdin;
        // otherwise calls the host and writes the same record to stdout.
        lines.push('  (func $protocol_get_datetime (result i64)');
        lines.push('    (local $val i64)');
        lines.push(`    (if (call $stdin_check_bytes (i32.const ${E.P_NOW}) (i32.const ${E.P_NOW_LEN}))`);
        lines.push('      (then');
        lines.push('        (local.set $val (call $stdin_parse_i64))');
        lines.push(`        (drop (call $stdin_check_bytes (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN})))`);
        lines.push('        (return (local.get $val))))');
        lines.push('    (local.set $val (call $host_now_millis))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_NOW}) (i32.const ${E.P_NOW_LEN}))`);
        lines.push('    (call $out_write_str (call $i64_to_str (local.get $val)))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN}))`);
        lines.push('    (local.get $val)');
        lines.push('  )');
        lines.push('');
        // $protocol_get_uuid — reads "uuid: <str>\n" if present in stdin;
        // otherwise calls the host and writes the same record to stdout.
        lines.push('  (func $protocol_get_uuid (result i32)');
        lines.push('    (local $val i32)');
        lines.push(`    (if (call $stdin_check_bytes (i32.const ${E.P_UUID}) (i32.const ${E.P_UUID_LEN}))`);
        lines.push('      (then (return (call $stdin_read_until_newline))))');
        lines.push('    (local.set $val (call $host_get_uuid))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_UUID}) (i32.const ${E.P_UUID_LEN}))`);
        lines.push('    (call $out_write_str (local.get $val))');
        lines.push(`    (call $out_write_raw (i32.const ${E.P_NL}) (i32.const ${E.P_NL_LEN}))`);
        lines.push('    (local.get $val)');
        lines.push('  )');
        lines.push('');
        return lines;
    }
    // ─── Built-in helpers ───────────────────────────────────────────────────────
    emitBuiltins() {
        const lines = [];
        lines.push('  ;; Built-in: str_concat');
        lines.push('  ;; Defensive: if either operand is null/0, substitute the empty');
        lines.push('  ;; string. Prevents the OOB-on-junk-pointer crash that surfaces');
        lines.push('  ;; when an upstream expression yielded an unresolved identifier.');
        lines.push('  (func $str_concat (param $a i32) (param $b i32) (result i32)');
        lines.push('    (local $alen i32) (local $blen i32) (local $total i32) (local $ptr i32)');
        lines.push(`    (if (i32.eqz (local.get $a)) (then (local.set $a (i32.const ${this.emptyStringAddr()}))))`);
        lines.push(`    (if (i32.eqz (local.get $b)) (then (local.set $b (i32.const ${this.emptyStringAddr()}))))`);
        lines.push('    (local.set $alen (i32.load (local.get $a)))');
        lines.push('    (local.set $blen (i32.load (local.get $b)))');
        lines.push('    (local.set $total (i32.add (local.get $alen) (local.get $blen)))');
        lines.push('    (local.set $ptr (call $alloc (i32.add (local.get $total) (i32.const 4))))');
        lines.push('    (i32.store (local.get $ptr) (local.get $total))');
        lines.push('    (memory.copy');
        lines.push('      (i32.add (local.get $ptr) (i32.const 4))');
        lines.push('      (i32.add (local.get $a) (i32.const 4))');
        lines.push('      (local.get $alen))');
        lines.push('    (memory.copy');
        lines.push('      (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $alen))');
        lines.push('      (i32.add (local.get $b) (i32.const 4))');
        lines.push('      (local.get $blen))');
        lines.push('    (local.get $ptr)');
        lines.push('  )');
        lines.push('');
        lines.push('  ;; Built-in: str_length');
        lines.push('  (func $str_length (param $s i32) (result i32)');
        lines.push('    (i32.load (local.get $s))');
        lines.push('  )');
        lines.push('');
        lines.push('  ;; Built-in: i32_to_str');
        lines.push('  (func $i32_to_str (param $n i32) (result i32)');
        lines.push('    (local $ptr i32) (local $tmp i32) (local $neg i32)');
        lines.push('    (local $buf i32) (local $len i32) (local $ch i32)');
        // Simple: allocate 16 bytes, format number
        lines.push('    (local.set $buf (call $alloc (i32.const 20)))');
        lines.push('    (local.set $len (i32.const 0))');
        lines.push('    (local.set $neg (i32.const 0))');
        lines.push('    (if (i32.lt_s (local.get $n) (i32.const 0)) (then');
        lines.push('      (local.set $neg (i32.const 1))');
        lines.push('      (local.set $n (i32.sub (i32.const 0) (local.get $n)))');
        lines.push('    ))');
        lines.push('    (if (i32.eqz (local.get $n)) (then');
        lines.push('      (i32.store8 (i32.add (local.get $buf) (i32.const 4)) (i32.const 48))');
        lines.push('      (local.set $len (i32.const 1))');
        lines.push('    ) (else');
        lines.push('      (local.set $tmp (local.get $n))');
        lines.push('      (block $done (loop $lp');
        lines.push('        (br_if $done (i32.eqz (local.get $tmp)))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $len))');
        lines.push('          (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        lines.push('        (local.set $tmp (i32.div_u (local.get $tmp) (i32.const 10)))');
        lines.push('        (local.set $len (i32.add (local.get $len) (i32.const 1)))');
        lines.push('        (br $lp)');
        lines.push('      ))');
        // Reverse digits
        lines.push('      (local.set $ptr (i32.const 0))');
        lines.push('      (block $rdone (loop $rlp');
        lines.push('        (br_if $rdone (i32.ge_u (local.get $ptr) (i32.div_u (local.get $len) (i32.const 2))))');
        lines.push('        (local.set $ch (i32.load8_u (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $ptr))))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $ptr))');
        lines.push('          (i32.load8_u (i32.add (i32.add (local.get $buf) (i32.const 4)) (i32.sub (i32.sub (local.get $len) (i32.const 1)) (local.get $ptr)))))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (i32.sub (i32.sub (local.get $len) (i32.const 1)) (local.get $ptr)))');
        lines.push('          (local.get $ch))');
        lines.push('        (local.set $ptr (i32.add (local.get $ptr) (i32.const 1)))');
        lines.push('        (br $rlp)');
        lines.push('      ))');
        lines.push('    ))');
        lines.push('    (i32.store (local.get $buf) (local.get $len))');
        lines.push('    (local.get $buf)');
        lines.push('  )');
        lines.push('');
        lines.push('  ;; Built-in: f64_to_str (very simplified)');
        lines.push('  (func $f64_to_str (param $n f64) (result i32)');
        // Convert to i32 and use i32_to_str as approximation
        lines.push('    (call $i32_to_str (i32.trunc_f64_s (local.get $n)))');
        lines.push('  )');
        lines.push('');
        // ── i64 → decimal FIL string ────────────────────────────────────────
        // Allocates 24 bytes (max i64 = 19 digits + sign), writes digits
        // backwards then reverses, sets the length prefix.
        lines.push('  ;; Built-in: i64_to_str');
        lines.push('  (func $i64_to_str (param $n i64) (result i32)');
        lines.push('    (local $buf i32) (local $len i32) (local $neg i32)');
        lines.push('    (local $i i32) (local $j i32) (local $ch i32)');
        lines.push('    (local.set $buf (call $alloc (i32.const 24)))');
        lines.push('    (local.set $len (i32.const 0))');
        lines.push('    (local.set $neg (i32.const 0))');
        lines.push('    (if (i64.lt_s (local.get $n) (i64.const 0)) (then');
        lines.push('      (local.set $neg (i32.const 1))');
        lines.push('      (local.set $n (i64.sub (i64.const 0) (local.get $n)))');
        lines.push('    ))');
        lines.push('    (if (i64.eqz (local.get $n)) (then');
        lines.push('      (i32.store8 (i32.add (local.get $buf) (i32.const 4)) (i32.const 48))');
        lines.push('      (local.set $len (i32.const 1))');
        lines.push('    ) (else');
        lines.push('      (block $done (loop $lp');
        lines.push('        (br_if $done (i64.eqz (local.get $n)))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $len))');
        lines.push('          (i32.add (i32.const 48) (i32.wrap_i64 (i64.rem_u (local.get $n) (i64.const 10)))))');
        lines.push('        (local.set $n (i64.div_u (local.get $n) (i64.const 10)))');
        lines.push('        (local.set $len (i32.add (local.get $len) (i32.const 1)))');
        lines.push('        (br $lp)');
        lines.push('      ))');
        lines.push('      (if (local.get $neg) (then');
        lines.push('        (i32.store8 (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $len)) (i32.const 45))'); // '-'
        lines.push('        (local.set $len (i32.add (local.get $len) (i32.const 1)))');
        lines.push('      ))');
        // Reverse the digits in place
        lines.push('      (local.set $i (i32.const 0))');
        lines.push('      (block $rdone (loop $rlp');
        lines.push('        (br_if $rdone (i32.ge_u (local.get $i) (i32.div_u (local.get $len) (i32.const 2))))');
        lines.push('        (local.set $j (i32.sub (i32.sub (local.get $len) (i32.const 1)) (local.get $i)))');
        lines.push('        (local.set $ch (i32.load8_u (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $i))))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $i))');
        lines.push('          (i32.load8_u (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $j))))');
        lines.push('        (i32.store8');
        lines.push('          (i32.add (i32.add (local.get $buf) (i32.const 4)) (local.get $j))');
        lines.push('          (local.get $ch))');
        lines.push('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        lines.push('        (br $rlp)');
        lines.push('      ))');
        lines.push('    ))');
        lines.push('    (i32.store (local.get $buf) (local.get $len))');
        lines.push('    (local.get $buf)');
        lines.push('  )');
        lines.push('');
        // ─── String equality ────────────────────────────────────────────────────
        lines.push('  ;; Built-in: str_eq (byte-by-byte FIL string comparison)');
        lines.push('  (func $str_eq (param $a i32) (param $b i32) (result i32)');
        lines.push('    (local $len i32) (local $i i32)');
        lines.push('    (if (i32.eq (local.get $a) (local.get $b)) (then (return (i32.const 1))))');
        lines.push('    (local.set $len (i32.load (local.get $a)))');
        lines.push('    (if (i32.ne (local.get $len) (i32.load (local.get $b))) (then (return (i32.const 0))))');
        lines.push('    (local.set $i (i32.const 0))');
        lines.push('    (block $done (loop $lp');
        lines.push('      (br_if $done (i32.ge_u (local.get $i) (local.get $len)))');
        lines.push('      (if (i32.ne');
        lines.push('        (i32.load8_u (i32.add (i32.add (local.get $a) (i32.const 4)) (local.get $i)))');
        lines.push('        (i32.load8_u (i32.add (i32.add (local.get $b) (i32.const 4)) (local.get $i))))');
        lines.push('        (then (return (i32.const 0))))');
        lines.push('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        lines.push('      (br $lp)');
        lines.push('    ))');
        lines.push('    (i32.const 1)');
        lines.push('  )');
        lines.push('');
        // Emit all JSON helpers (Phase 2)
        lines.push(...this.emitJsonHelpers());
        // Emit DateTime helpers (calendar arithmetic + ISO formatting/parsing)
        lines.push(...this.emitDateTimeHelpers());
        return lines;
    }
    // ─── DateTime helpers (Phase 2) ─────────────────────────────────────────
    //
    // All times are UTC. Values are i64 Unix epoch milliseconds.
    // Calendar conversions use Howard Hinnant's days_from_civil / civil_from_days
    // (https://howardhinnant.github.io/date_algorithms.html), which gives correct
    // results for all dates including the proleptic Gregorian calendar.
    //
    // Year-of-month-of-day decomposition is cached via $_dt_ymd_cache so that
    // back-to-back calls to getYear/getMonth/getDay (and toISOString) on the
    // same DateTime do not redo the algorithm.
    emitDateTimeHelpers() {
        const L = [];
        const p = (s) => L.push(s);
        p('  ;; ── DateTime helpers (Phase 2) ─────────────────────────────────');
        p('');
        // Globals for cached year/month/day decomposition
        p('  (global $_dt_year (mut i32) (i32.const 0))');
        p('  (global $_dt_month (mut i32) (i32.const 0))');
        p('  (global $_dt_day (mut i32) (i32.const 0))');
        p('  ;; cache key — sentinel chosen to never match a real epoch_ms');
        p('  (global $_dt_ymd_cache (mut i64) (i64.const -9223372036854775807))');
        p('');
        // Floor division of i64 by 86_400_000 (ms per day).
        // i64.div_s rounds toward zero; we adjust for negative remainder.
        p('  (func $_dt_floor_days (param $ms i64) (result i64)');
        p('    (local $q i64) (local $r i64)');
        p('    (local.set $q (i64.div_s (local.get $ms) (i64.const 86400000)))');
        p('    (local.set $r (i64.rem_s (local.get $ms) (i64.const 86400000)))');
        p('    (if (i64.lt_s (local.get $r) (i64.const 0)) (then');
        p('      (local.set $q (i64.sub (local.get $q) (i64.const 1)))))');
        p('    (local.get $q)');
        p('  )');
        p('');
        // Returns ms-of-day in [0, 86_400_000) as i32. Always non-negative.
        p('  (func $_dt_ms_of_day (param $ms i64) (result i32)');
        p('    (local $r i64)');
        p('    (local.set $r (i64.rem_s (local.get $ms) (i64.const 86400000)))');
        p('    (if (i64.lt_s (local.get $r) (i64.const 0)) (then');
        p('      (local.set $r (i64.add (local.get $r) (i64.const 86400000)))))');
        p('    (i32.wrap_i64 (local.get $r))');
        p('  )');
        p('');
        // civil_from_days: populates $_dt_year, $_dt_month, $_dt_day.
        p('  (func $_dt_compute_ymd (param $ms i64)');
        p('    (local $days i64) (local $z i64) (local $era i64) (local $doe i64)');
        p('    (local $yoe i64) (local $y i64) (local $doy i64) (local $mp i64)');
        p('    (local $d i64) (local $m i64)');
        p('    (if (i64.eq (local.get $ms) (global.get $_dt_ymd_cache)) (then (return)))');
        p('    (global.set $_dt_ymd_cache (local.get $ms))');
        p('    (local.set $days (call $_dt_floor_days (local.get $ms)))');
        p('    (local.set $z (i64.add (local.get $days) (i64.const 719468)))');
        // era = (z >= 0 ? z : z - 146096) / 146097
        p('    (if (i64.lt_s (local.get $z) (i64.const 0))');
        p('      (then (local.set $era (i64.div_s (i64.sub (local.get $z) (i64.const 146096)) (i64.const 146097))))');
        p('      (else (local.set $era (i64.div_s (local.get $z) (i64.const 146097)))))');
        // doe = z - era * 146097
        p('    (local.set $doe (i64.sub (local.get $z) (i64.mul (local.get $era) (i64.const 146097))))');
        // yoe = (doe - doe/1460 + doe/36524 - doe/146096) / 365
        p('    (local.set $yoe (i64.div_u');
        p('      (i64.sub');
        p('        (i64.add');
        p('          (i64.sub (local.get $doe) (i64.div_u (local.get $doe) (i64.const 1460)))');
        p('          (i64.div_u (local.get $doe) (i64.const 36524)))');
        p('        (i64.div_u (local.get $doe) (i64.const 146096)))');
        p('      (i64.const 365)))');
        // y = yoe + era * 400
        p('    (local.set $y (i64.add (local.get $yoe) (i64.mul (local.get $era) (i64.const 400))))');
        // doy = doe - (365*yoe + yoe/4 - yoe/100)
        p('    (local.set $doy (i64.sub (local.get $doe)');
        p('      (i64.sub');
        p('        (i64.add (i64.mul (i64.const 365) (local.get $yoe)) (i64.div_u (local.get $yoe) (i64.const 4)))');
        p('        (i64.div_u (local.get $yoe) (i64.const 100)))))');
        // mp = (5*doy + 2) / 153
        p('    (local.set $mp (i64.div_u');
        p('      (i64.add (i64.mul (i64.const 5) (local.get $doy)) (i64.const 2))');
        p('      (i64.const 153)))');
        // d = doy - (153*mp + 2)/5 + 1
        p('    (local.set $d (i64.add');
        p('      (i64.sub (local.get $doy)');
        p('        (i64.div_u');
        p('          (i64.add (i64.mul (i64.const 153) (local.get $mp)) (i64.const 2))');
        p('          (i64.const 5)))');
        p('      (i64.const 1)))');
        // m = mp < 10 ? mp + 3 : mp - 9
        p('    (if (i64.lt_s (local.get $mp) (i64.const 10))');
        p('      (then (local.set $m (i64.add (local.get $mp) (i64.const 3))))');
        p('      (else (local.set $m (i64.sub (local.get $mp) (i64.const 9)))))');
        // y = m <= 2 ? y + 1 : y
        p('    (if (i64.le_s (local.get $m) (i64.const 2))');
        p('      (then (local.set $y (i64.add (local.get $y) (i64.const 1)))))');
        p('    (global.set $_dt_year (i32.wrap_i64 (local.get $y)))');
        p('    (global.set $_dt_month (i32.wrap_i64 (local.get $m)))');
        p('    (global.set $_dt_day (i32.wrap_i64 (local.get $d)))');
        p('  )');
        p('');
        // Public getters
        p('  (func $datetime_year (param $ms i64) (result i32)');
        p('    (call $_dt_compute_ymd (local.get $ms))');
        p('    (global.get $_dt_year)');
        p('  )');
        p('');
        p('  (func $datetime_month (param $ms i64) (result i32)');
        p('    (call $_dt_compute_ymd (local.get $ms))');
        p('    (global.get $_dt_month)');
        p('  )');
        p('');
        p('  (func $datetime_day (param $ms i64) (result i32)');
        p('    (call $_dt_compute_ymd (local.get $ms))');
        p('    (global.get $_dt_day)');
        p('  )');
        p('');
        p('  (func $datetime_dayofweek (param $ms i64) (result i32)');
        p('    (local $days i64) (local $r i32)');
        p('    (local.set $days (call $_dt_floor_days (local.get $ms)))');
        // (days + 4) mod 7, normalised to [0, 6]. 1970-01-01 was a Thursday (=4).
        p('    (local.set $r (i32.wrap_i64 (i64.rem_s (i64.add (local.get $days) (i64.const 4)) (i64.const 7))))');
        p('    (if (i32.lt_s (local.get $r) (i32.const 0)) (then');
        p('      (local.set $r (i32.add (local.get $r) (i32.const 7)))))');
        p('    (local.get $r)');
        p('  )');
        p('');
        p('  (func $datetime_hour (param $ms i64) (result i32)');
        p('    (i32.div_u (call $_dt_ms_of_day (local.get $ms)) (i32.const 3600000))');
        p('  )');
        p('');
        p('  (func $datetime_minute (param $ms i64) (result i32)');
        p('    (i32.rem_u (i32.div_u (call $_dt_ms_of_day (local.get $ms)) (i32.const 60000)) (i32.const 60))');
        p('  )');
        p('');
        p('  (func $datetime_second (param $ms i64) (result i32)');
        p('    (i32.rem_u (i32.div_u (call $_dt_ms_of_day (local.get $ms)) (i32.const 1000)) (i32.const 60))');
        p('  )');
        p('');
        p('  (func $datetime_millisecond (param $ms i64) (result i32)');
        p('    (i32.rem_u (call $_dt_ms_of_day (local.get $ms)) (i32.const 1000))');
        p('  )');
        p('');
        // toISOString: format as YYYY-MM-DDTHH:MM:SS.mmmZ (24 bytes, no length prefix yet).
        // Assumes year fits in 4 digits — adequate for 1..9999.
        p('  ;; datetime_to_iso: i64 epoch ms → FIL string "YYYY-MM-DDTHH:MM:SS.mmmZ"');
        p('  (func $datetime_to_iso (param $ms i64) (result i32)');
        p('    (local $buf i32) (local $base i32) (local $y i32) (local $tmp i32)');
        p('    (local.set $buf (call $alloc (i32.const 28)))');
        p('    (local.set $base (i32.add (local.get $buf) (i32.const 4)))');
        p('    (call $_dt_compute_ymd (local.get $ms))');
        // Year (4 digits)
        p('    (local.set $y (global.get $_dt_year))');
        p('    (i32.store8 offset=0 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $y) (i32.const 1000))))');
        p('    (local.set $tmp (i32.rem_u (local.get $y) (i32.const 1000)))');
        p('    (i32.store8 offset=1 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 100))))');
        p('    (local.set $tmp (i32.rem_u (local.get $tmp) (i32.const 100)))');
        p('    (i32.store8 offset=2 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=3 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=4 (local.get $base) (i32.const 45))'); // -
        // Month
        p('    (local.set $tmp (global.get $_dt_month))');
        p('    (i32.store8 offset=5 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=6 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=7 (local.get $base) (i32.const 45))'); // -
        // Day
        p('    (local.set $tmp (global.get $_dt_day))');
        p('    (i32.store8 offset=8 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=9 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=10 (local.get $base) (i32.const 84))'); // T
        // Hour
        p('    (local.set $tmp (call $datetime_hour (local.get $ms)))');
        p('    (i32.store8 offset=11 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=12 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=13 (local.get $base) (i32.const 58))'); // :
        // Minute
        p('    (local.set $tmp (call $datetime_minute (local.get $ms)))');
        p('    (i32.store8 offset=14 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=15 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=16 (local.get $base) (i32.const 58))'); // :
        // Second
        p('    (local.set $tmp (call $datetime_second (local.get $ms)))');
        p('    (i32.store8 offset=17 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=18 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=19 (local.get $base) (i32.const 46))'); // .
        // Millisecond
        p('    (local.set $tmp (call $datetime_millisecond (local.get $ms)))');
        p('    (i32.store8 offset=20 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 100))))');
        p('    (local.set $tmp (i32.rem_u (local.get $tmp) (i32.const 100)))');
        p('    (i32.store8 offset=21 (local.get $base) (i32.add (i32.const 48) (i32.div_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=22 (local.get $base) (i32.add (i32.const 48) (i32.rem_u (local.get $tmp) (i32.const 10))))');
        p('    (i32.store8 offset=23 (local.get $base) (i32.const 90))'); // Z
        p('    (i32.store (local.get $buf) (i32.const 24))');
        p('    (local.get $buf)');
        p('  )');
        p('');
        // days_from_civil: the inverse of civil_from_days. Returns days since 1970-01-01.
        p('  (func $_dt_days_from_civil (param $y i64) (param $m i64) (param $d i64) (result i64)');
        p('    (local $era i64) (local $yoe i64) (local $mAdj i64) (local $doy i64) (local $doe i64)');
        // y -= m <= 2
        p('    (if (i64.le_s (local.get $m) (i64.const 2)) (then');
        p('      (local.set $y (i64.sub (local.get $y) (i64.const 1)))))');
        // era = (y >= 0 ? y : y-399) / 400
        p('    (if (i64.lt_s (local.get $y) (i64.const 0))');
        p('      (then (local.set $era (i64.div_s (i64.sub (local.get $y) (i64.const 399)) (i64.const 400))))');
        p('      (else (local.set $era (i64.div_s (local.get $y) (i64.const 400)))))');
        // yoe = y - era * 400
        p('    (local.set $yoe (i64.sub (local.get $y) (i64.mul (local.get $era) (i64.const 400))))');
        // mAdj = m > 2 ? m-3 : m+9
        p('    (if (i64.gt_s (local.get $m) (i64.const 2))');
        p('      (then (local.set $mAdj (i64.sub (local.get $m) (i64.const 3))))');
        p('      (else (local.set $mAdj (i64.add (local.get $m) (i64.const 9)))))');
        // doy = (153*mAdj + 2)/5 + d - 1
        p('    (local.set $doy (i64.add');
        p('      (i64.sub');
        p('        (i64.div_u (i64.add (i64.mul (i64.const 153) (local.get $mAdj)) (i64.const 2)) (i64.const 5))');
        p('        (i64.const 1))');
        p('      (local.get $d)))');
        // doe = yoe*365 + yoe/4 - yoe/100 + doy
        p('    (local.set $doe (i64.add');
        p('      (i64.sub');
        p('        (i64.add (i64.mul (local.get $yoe) (i64.const 365)) (i64.div_u (local.get $yoe) (i64.const 4)))');
        p('        (i64.div_u (local.get $yoe) (i64.const 100)))');
        p('      (local.get $doy)))');
        // return era*146097 + doe - 719468
        p('    (i64.sub');
        p('      (i64.add (i64.mul (local.get $era) (i64.const 146097)) (local.get $doe))');
        p('      (i64.const 719468))');
        p('  )');
        p('');
        // datetime_parse_iso: FIL string ptr → i64 epoch ms.
        // Accepted format: YYYY-MM-DDTHH:MM:SS[.mmm]Z. Other separators/trailing
        // characters return 0. The parser is intentionally strict.
        p('  ;; Parse N decimal digits from a memory address into an i32.');
        p('  (func $_dt_read_digits (param $addr i32) (param $n i32) (result i32)');
        p('    (local $r i32) (local $i i32) (local $c i32)');
        p('    (local.set $r (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $i) (local.get $n)))');
        p('      (local.set $c (i32.load8_u (i32.add (local.get $addr) (local.get $i))))');
        p('      (local.set $r (i32.add (i32.mul (local.get $r) (i32.const 10))');
        p('                             (i32.sub (local.get $c) (i32.const 48))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $lp)))');
        p('    (local.get $r)');
        p('  )');
        p('');
        p('  (func $datetime_parse_iso (param $str i32) (result i64)');
        p('    (local $base i32) (local $len i32)');
        p('    (local $y i32) (local $mo i32) (local $d i32)');
        p('    (local $h i32) (local $mi i32) (local $s i32) (local $ms i32)');
        p('    (local $days i64) (local $epoch i64)');
        p('    (local.set $len (i32.load (local.get $str)))');
        p('    (local.set $base (i32.add (local.get $str) (i32.const 4)))');
        p('    (if (i32.lt_u (local.get $len) (i32.const 19))');
        p('      (then (return (i64.const 0))))');
        p('    (local.set $y (call $_dt_read_digits (local.get $base) (i32.const 4)))');
        p('    (local.set $mo (call $_dt_read_digits (i32.add (local.get $base) (i32.const 5)) (i32.const 2)))');
        p('    (local.set $d (call $_dt_read_digits (i32.add (local.get $base) (i32.const 8)) (i32.const 2)))');
        p('    (local.set $h (call $_dt_read_digits (i32.add (local.get $base) (i32.const 11)) (i32.const 2)))');
        p('    (local.set $mi (call $_dt_read_digits (i32.add (local.get $base) (i32.const 14)) (i32.const 2)))');
        p('    (local.set $s (call $_dt_read_digits (i32.add (local.get $base) (i32.const 17)) (i32.const 2)))');
        p('    (local.set $ms (i32.const 0))');
        // If a '.' follows, read up to 3 ms digits
        p('    (if (i32.ge_u (local.get $len) (i32.const 23))');
        p('      (then');
        p('        (if (i32.eq (i32.load8_u (i32.add (local.get $base) (i32.const 19))) (i32.const 46))');
        p('          (then');
        p('            (local.set $ms (call $_dt_read_digits (i32.add (local.get $base) (i32.const 20)) (i32.const 3)))))))');
        // days_from_civil
        p('    (local.set $days (call $_dt_days_from_civil');
        p('      (i64.extend_i32_s (local.get $y))');
        p('      (i64.extend_i32_s (local.get $mo))');
        p('      (i64.extend_i32_s (local.get $d))))');
        // epoch_ms = days*86400000 + h*3600000 + mi*60000 + s*1000 + ms
        p('    (local.set $epoch (i64.mul (local.get $days) (i64.const 86400000)))');
        p('    (local.set $epoch (i64.add (local.get $epoch)');
        p('      (i64.mul (i64.extend_i32_s (local.get $h)) (i64.const 3600000))))');
        p('    (local.set $epoch (i64.add (local.get $epoch)');
        p('      (i64.mul (i64.extend_i32_s (local.get $mi)) (i64.const 60000))))');
        p('    (local.set $epoch (i64.add (local.get $epoch)');
        p('      (i64.mul (i64.extend_i32_s (local.get $s)) (i64.const 1000))))');
        p('    (local.set $epoch (i64.add (local.get $epoch) (i64.extend_i32_s (local.get $ms))))');
        p('    (local.get $epoch)');
        p('  )');
        p('');
        return L;
    }
    // ─── JSON helpers (Phase 2) ──────────────────────────────────────────────
    // Tags: STRING=0, NUMBER=1, TRUE=2, FALSE=3, NULL=4, OBJECT=5, ARRAY=6
    // Object layout: [count:i32, (key:i32, val:i32, tag:i32)*] — 12 bytes/entry
    // Array layout:  [count:i32, (val:i32, tag:i32)*] — 8 bytes/element
    emitJsonHelpers() {
        const L = [];
        const p = (s) => L.push(s);
        // Pre-intern delimiter strings used by stringify
        const sLBrace = this.internString('{');
        const sRBrace = this.internString('}');
        const sLBracket = this.internString('[');
        const sRBracket = this.internString(']');
        const sQuote = this.internString('"');
        const sColonQuote = this.internString('":"');
        const sComma = this.internString(',');
        const sCommaQuote = this.internString(',"');
        const sColon = this.internString(':');
        const sTrue = this.internString('true');
        const sFalse = this.internString('false');
        const sNull = this.internString('null');
        const sEmptyObj = this.internString('{}');
        const sEmptyArr = this.internString('[]');
        const sCloseKeyColon = this.internString('":');
        const sBackslashQ = this.internString('\\"');
        const sBackslashB = this.internString('\\\\');
        const sBackslashN = this.internString('\\n');
        const sBackslashR = this.internString('\\r');
        const sBackslashT = this.internString('\\t');
        const emptyStr = this.emptyStringAddr();
        // ── Object table ──────────────────────────────────────────────────────
        p('  ;; json_obj_new: allocate object table (12 bytes per entry)');
        p('  (func $json_obj_new (param $count i32) (result i32)');
        p('    (local $ptr i32)');
        p('    (local.set $ptr (call $alloc (i32.add (i32.const 4) (i32.mul (local.get $count) (i32.const 12)))))');
        p('    (i32.store (local.get $ptr) (local.get $count))');
        p('    (local.get $ptr)');
        p('  )');
        p('');
        p('  ;; json_obj_set: set key/val/tag at index (12-byte stride)');
        p('  (func $json_obj_set (param $obj i32) (param $idx i32) (param $key i32) (param $val i32) (param $tag i32)');
        p('    (local $base i32)');
        p('    (local.set $base (i32.add (local.get $obj) (i32.add (i32.const 4) (i32.mul (local.get $idx) (i32.const 12)))))');
        p('    (i32.store (local.get $base) (local.get $key))');
        p('    (i32.store offset=4 (local.get $base) (local.get $val))');
        p('    (i32.store offset=8 (local.get $base) (local.get $tag))');
        p('  )');
        p('');
        p('  ;; json_get_field: linear scan, returns val (or empty string if not');
        p('  ;; found). Defensive: if $obj is below HEAP_START it is not a real');
        p('  ;; allocated object table — return emptyStr instead of dereferencing.');
        p('  ;; This protects against unresolved identifiers (e.g. v1 `$vars` names');
        p('  ;; surviving into a sync helper body) that emit `(i32.const <small>)`.');
        p('  ;; Also sets $_json_tag to the stored tag of the matching entry (or 4=');
        p('  ;; NULL when no match), so consumers like $_json_value_to_str and');
        p('  ;; $_json_stringify_value can dispatch correctly without the caller');
        p('  ;; needing to thread the tag manually.');
        p('  (func $json_get_field (param $obj i32) (param $key i32) (result i32)');
        p('    (local $count i32) (local $i i32) (local $base i32)');
        p(`    (if (i32.lt_u (local.get $obj) (i32.const ${WatEmitter.HEAP_START}))`);
        p('      (then (global.set $_json_tag (i32.const 4))');
        p(`            (return (i32.const ${emptyStr}))))`);
        p('    (local.set $count (i32.load (local.get $obj)))');
        p('    (local.set $i (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $i) (local.get $count)))');
        p('      (local.set $base (i32.add (local.get $obj) (i32.add (i32.const 4) (i32.mul (local.get $i) (i32.const 12)))))');
        p('      (if (call $str_eq (i32.load (local.get $base)) (local.get $key))');
        p('        (then (global.set $_json_tag (i32.load offset=8 (local.get $base)))');
        p('              (return (i32.load offset=4 (local.get $base)))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p('    (global.set $_json_tag (i32.const 4))');
        p(`    (i32.const ${emptyStr})`);
        p('  )');
        p('');
        // ── Array table ───────────────────────────────────────────────────────
        p('  ;; json_array_new: allocate array table (8 bytes per element)');
        p('  (func $json_array_new (param $count i32) (result i32)');
        p('    (local $ptr i32)');
        p('    (local.set $ptr (call $alloc (i32.add (i32.const 4) (i32.mul (local.get $count) (i32.const 8)))))');
        p('    (i32.store (local.get $ptr) (local.get $count))');
        p('    (local.get $ptr)');
        p('  )');
        p('');
        p('  ;; json_array_set: set val/tag at index (8-byte stride)');
        p('  (func $json_array_set (param $arr i32) (param $idx i32) (param $val i32) (param $tag i32)');
        p('    (local $base i32)');
        p('    (local.set $base (i32.add (local.get $arr) (i32.add (i32.const 4) (i32.mul (local.get $idx) (i32.const 8)))))');
        p('    (i32.store (local.get $base) (local.get $val))');
        p('    (i32.store offset=4 (local.get $base) (local.get $tag))');
        p('  )');
        p('');
        p('  ;; json_array_get: get val at index, also setting $_json_tag to the');
        p('  ;; stored tag of the element. Needed so downstream consumers (for-of');
        p('  ;; bodies, JSON.stringify) dispatch on the element\'s real type rather');
        p('  ;; than a stale global from the parent collection.');
        p('  (func $json_array_get (param $arr i32) (param $idx i32) (result i32)');
        p('    (local $base i32)');
        p('    (local.set $base (i32.add (local.get $arr) (i32.add (i32.const 4) (i32.mul (local.get $idx) (i32.const 8)))))');
        p('    (global.set $_json_tag (i32.load offset=4 (local.get $base)))');
        p('    (i32.load (local.get $base))');
        p('  )');
        p('');
        p('  ;; json_array_len: get count');
        p('  (func $json_array_len (param $arr i32) (result i32)');
        p('    (i32.load (local.get $arr))');
        p('  )');
        p('');
        // ── Parser: skip whitespace ───────────────────────────────────────────
        p('  ;; _json_skip_ws: advance $_json_pos past spaces/tabs/CR/LF');
        p('  (func $_json_skip_ws');
        p('    (local $ch i32)');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (global.get $_json_pos) (global.get $_json_len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        p('      (br_if $done (i32.and (i32.ne (local.get $ch) (i32.const 0x20))'); // space
        p('        (i32.and (i32.ne (local.get $ch) (i32.const 0x09))'); // tab
        p('          (i32.and (i32.ne (local.get $ch) (i32.const 0x0A))'); // LF
        p('            (i32.ne (local.get $ch) (i32.const 0x0D))))))'); // CR
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p('  )');
        p('');
        // ── Parser: count entries at current depth ────────────────────────────
        // Saves/restores $_json_pos. Counts commas at depth 0.
        // Returns 0 for empty container, commas+1 otherwise.
        p('  ;; _json_count_entries: count elements/pairs in current container');
        p('  (func $_json_count_entries (param $close i32) (result i32)');
        p('    (local $save i32) (local $count i32) (local $depth i32)');
        p('    (local $in_str i32) (local $ch i32) (local $has_content i32)');
        p('    (local.set $save (global.get $_json_pos))');
        p('    (call $_json_skip_ws)');
        // Check for immediate close
        p('    (if (i32.lt_u (global.get $_json_pos) (global.get $_json_len))');
        p('      (then (if (i32.eq (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))) (local.get $close))');
        p('        (then (global.set $_json_pos (local.get $save)) (return (i32.const 0))))))');
        p('    (local.set $count (i32.const 0))');
        p('    (local.set $depth (i32.const 0))');
        p('    (local.set $in_str (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (global.get $_json_pos) (global.get $_json_len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        // In string: handle escapes and closing quote
        p('      (if (local.get $in_str) (then');
        p('        (if (i32.eq (local.get $ch) (i32.const 0x5C))'); // backslash
        p('          (then (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 2))) (br $lp)))');
        p('        (if (i32.eq (local.get $ch) (i32.const 0x22))'); // quote
        p('          (then (local.set $in_str (i32.const 0))))');
        p('        (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('        (br $lp)');
        p('      ))');
        // Not in string
        p('      (if (i32.eq (local.get $ch) (i32.const 0x22))');
        p('        (then (local.set $in_str (i32.const 1)) (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1))) (br $lp)))');
        p('      (if (i32.or (i32.eq (local.get $ch) (i32.const 0x7B)) (i32.eq (local.get $ch) (i32.const 0x5B)))');
        p('        (then (local.set $depth (i32.add (local.get $depth) (i32.const 1)))');
        p('          (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1))) (br $lp)))');
        p('      (if (i32.or (i32.eq (local.get $ch) (i32.const 0x7D)) (i32.eq (local.get $ch) (i32.const 0x5D)))');
        p('        (then (if (i32.eqz (local.get $depth)) (then (br $done)))');
        p('          (local.set $depth (i32.sub (local.get $depth) (i32.const 1)))');
        p('          (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1))) (br $lp)))');
        p('      (if (i32.and (i32.eq (local.get $ch) (i32.const 0x2C)) (i32.eqz (local.get $depth)))');
        p('        (then (local.set $count (i32.add (local.get $count) (i32.const 1)))))');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p('    (global.set $_json_pos (local.get $save))');
        p('    (i32.add (local.get $count) (i32.const 1))');
        p('  )');
        p('');
        // ── Parser: parse string with escape handling ─────────────────────────
        // $_json_pos at opening '"'. Sets $_json_tag = 0 (STRING).
        p('  ;; _json_parse_string: parse "..." with escape handling → FIL string');
        p('  (func $_json_parse_string (result i32)');
        p('    (local $start i32) (local $scan i32) (local $ch i32)');
        p('    (local $max_len i32) (local $ptr i32) (local $dst i32) (local $next i32)');
        // Skip opening "
        p('    (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        // Scan forward to find closing " (for max allocation size)
        p('    (local.set $scan (global.get $_json_pos))');
        p('    (block $sd (loop $sl');
        p('      (br_if $sd (i32.ge_u (local.get $scan) (global.get $_json_len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (local.get $scan))))');
        p('      (if (i32.eq (local.get $ch) (i32.const 0x5C))');
        p('        (then (local.set $scan (i32.add (local.get $scan) (i32.const 2))) (br $sl)))');
        p('      (br_if $sd (i32.eq (local.get $ch) (i32.const 0x22)))');
        p('      (local.set $scan (i32.add (local.get $scan) (i32.const 1)))');
        p('      (br $sl)');
        p('    ))');
        // Allocate max possible size
        p('    (local.set $max_len (i32.sub (local.get $scan) (global.get $_json_pos)))');
        p('    (local.set $ptr (call $alloc (i32.add (local.get $max_len) (i32.const 4))))');
        p('    (local.set $dst (i32.const 0))');
        // Copy with escape handling
        p('    (block $cd (loop $cl');
        p('      (br_if $cd (i32.ge_u (global.get $_json_pos) (global.get $_json_len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        p('      (br_if $cd (i32.eq (local.get $ch) (i32.const 0x22)))'); // closing "
        p('      (if (i32.eq (local.get $ch) (i32.const 0x5C)) (then');
        p('        (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('        (local.set $next (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        p('        (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        // Decode escape → store decoded byte in $ch (reuse local)
        p('        (local.set $ch (local.get $next))'); // default: literal char
        p('        (if (i32.eq (local.get $next) (i32.const 0x6E)) (then (local.set $ch (i32.const 0x0A))))'); // \n
        p('        (if (i32.eq (local.get $next) (i32.const 0x72)) (then (local.set $ch (i32.const 0x0D))))'); // \r
        p('        (if (i32.eq (local.get $next) (i32.const 0x74)) (then (local.set $ch (i32.const 0x09))))'); // \t
        p('        (if (i32.eq (local.get $next) (i32.const 0x62)) (then (local.set $ch (i32.const 0x08))))'); // \b
        p('        (if (i32.eq (local.get $next) (i32.const 0x66)) (then (local.set $ch (i32.const 0x0C))))'); // \f
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (local.get $ch))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (br $cl)');
        p('      ))');
        // Normal byte
        p('      (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (local.get $ch))');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('      (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('      (br $cl)');
        p('    ))');
        // Skip closing "
        p('    (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        // Set actual length
        p('    (i32.store (local.get $ptr) (local.get $dst))');
        p('    (global.set $_json_tag (i32.const 0))');
        p('    (local.get $ptr)');
        p('  )');
        p('');
        // ── Parser: parse number → FIL string ─────────────────────────────────
        p('  ;; _json_parse_number: parse number literal → FIL string, tag=1');
        p('  (func $_json_parse_number (result i32)');
        p('    (local $start i32) (local $ch i32) (local $len i32) (local $ptr i32)');
        p('    (local.set $start (global.get $_json_pos))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (global.get $_json_pos) (global.get $_json_len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        // Continue on digits, dot, minus, plus, e, E
        p('      (br_if $done (i32.and');
        p('        (i32.and (i32.lt_u (local.get $ch) (i32.const 0x2B)) (i32.ne (local.get $ch) (i32.const 0x2D)))'); // not +,-
        p('        (i32.const 1)))'); // always true (just need the and)
        // Simpler: break if char is not in [0-9.eE+-]
        p('      (br_if $done (i32.and');
        p('        (i32.and');
        p('          (i32.or (i32.lt_u (local.get $ch) (i32.const 0x30)) (i32.gt_u (local.get $ch) (i32.const 0x39)))'); // not 0-9
        p('          (i32.ne (local.get $ch) (i32.const 0x2E)))'); // not .
        p('        (i32.and');
        p('          (i32.and (i32.ne (local.get $ch) (i32.const 0x65)) (i32.ne (local.get $ch) (i32.const 0x45)))'); // not e,E
        p('          (i32.and (i32.ne (local.get $ch) (i32.const 0x2B)) (i32.ne (local.get $ch) (i32.const 0x2D))))))'); // not +,-
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p('    (local.set $len (i32.sub (global.get $_json_pos) (local.get $start)))');
        p('    (local.set $ptr (call $alloc (i32.add (local.get $len) (i32.const 4))))');
        p('    (i32.store (local.get $ptr) (local.get $len))');
        p('    (memory.copy (i32.add (local.get $ptr) (i32.const 4)) (i32.add (global.get $_json_data) (local.get $start)) (local.get $len))');
        p('    (global.set $_json_tag (i32.const 1))');
        p('    (local.get $ptr)');
        p('  )');
        p('');
        // ── Parser: parse any value ───────────────────────────────────────────
        // Dispatches on first non-whitespace byte. Sets $_json_tag.
        p('  ;; _json_parse_value: parse any JSON value, sets $_json_tag');
        p('  (func $_json_parse_value (result i32)');
        p('    (local $ch i32)');
        p('    (call $_json_skip_ws)');
        p('    (if (i32.ge_u (global.get $_json_pos) (global.get $_json_len))');
        p(`      (then (global.set $_json_tag (i32.const 4)) (return (i32.const ${emptyStr}))))`);
        p('    (local.set $ch (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))))');
        // " → string
        p('    (if (i32.eq (local.get $ch) (i32.const 0x22)) (then (return (call $_json_parse_string))))');
        // { → object
        p('    (if (i32.eq (local.get $ch) (i32.const 0x7B)) (then (return (call $_json_parse_object))))');
        // [ → array
        p('    (if (i32.eq (local.get $ch) (i32.const 0x5B)) (then (return (call $_json_parse_array))))');
        // t → true
        p('    (if (i32.eq (local.get $ch) (i32.const 0x74)) (then');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 4)))');
        p('      (global.set $_json_tag (i32.const 2))');
        p(`      (return (i32.const ${sTrue}))))`);
        // f → false
        p('    (if (i32.eq (local.get $ch) (i32.const 0x66)) (then');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 5)))');
        p('      (global.set $_json_tag (i32.const 3))');
        p(`      (return (i32.const ${sFalse}))))`);
        // n → null
        p('    (if (i32.eq (local.get $ch) (i32.const 0x6E)) (then');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 4)))');
        p('      (global.set $_json_tag (i32.const 4))');
        p(`      (return (i32.const ${emptyStr}))))`);
        // digit or minus → number
        p('    (if (i32.or (i32.and (i32.ge_u (local.get $ch) (i32.const 0x30)) (i32.le_u (local.get $ch) (i32.const 0x39)))');
        p('      (i32.eq (local.get $ch) (i32.const 0x2D)))');
        p('      (then (return (call $_json_parse_number))))');
        // Unknown → skip byte, return empty
        p('    (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        p('    (global.set $_json_tag (i32.const 4))');
        p(`    (i32.const ${emptyStr})`);
        p('  )');
        p('');
        // ── Parser: parse object ──────────────────────────────────────────────
        p('  ;; _json_parse_object: parse {...} → json object table, tag=5');
        p('  (func $_json_parse_object (result i32)');
        p('    (local $count i32) (local $obj i32) (local $idx i32)');
        p('    (local $key i32) (local $val i32) (local $tag i32) (local $ch i32)');
        // Skip opening {
        p('    (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        // Pre-count entries
        p('    (local.set $count (call $_json_count_entries (i32.const 0x7D)))'); // }
        p('    (local.set $obj (call $json_obj_new (local.get $count)))');
        p('    (local.set $idx (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $idx) (local.get $count)))');
        p('      (call $_json_skip_ws)');
        // Parse key (must be a string)
        p('      (local.set $key (call $_json_parse_string))');
        // Skip : with whitespace
        p('      (call $_json_skip_ws)');
        p('      (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))'); // skip ':'
        // Parse value
        p('      (local.set $val (call $_json_parse_value))');
        p('      (local.set $tag (global.get $_json_tag))');
        // Store in table
        p('      (call $json_obj_set (local.get $obj) (local.get $idx) (local.get $key) (local.get $val) (local.get $tag))');
        // Skip comma if present
        p('      (call $_json_skip_ws)');
        p('      (if (i32.lt_u (global.get $_json_pos) (global.get $_json_len))');
        p('        (then (if (i32.eq (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))) (i32.const 0x2C))');
        p('          (then (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))))))');
        p('      (local.set $idx (i32.add (local.get $idx) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        // Skip closing }
        p('    (call $_json_skip_ws)');
        p('    (if (i32.lt_u (global.get $_json_pos) (global.get $_json_len))');
        p('      (then (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))))');
        p('    (global.set $_json_tag (i32.const 5))');
        p('    (local.get $obj)');
        p('  )');
        p('');
        // ── Parser: parse array ───────────────────────────────────────────────
        p('  ;; _json_parse_array: parse [...] → json array table, tag=6');
        p('  (func $_json_parse_array (result i32)');
        p('    (local $count i32) (local $arr i32) (local $idx i32)');
        p('    (local $val i32) (local $tag i32)');
        // Skip opening [
        p('    (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))');
        // Pre-count elements
        p('    (local.set $count (call $_json_count_entries (i32.const 0x5D)))'); // ]
        p('    (local.set $arr (call $json_array_new (local.get $count)))');
        p('    (local.set $idx (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $idx) (local.get $count)))');
        p('      (local.set $val (call $_json_parse_value))');
        p('      (local.set $tag (global.get $_json_tag))');
        p('      (call $json_array_set (local.get $arr) (local.get $idx) (local.get $val) (local.get $tag))');
        // Skip comma if present
        p('      (call $_json_skip_ws)');
        p('      (if (i32.lt_u (global.get $_json_pos) (global.get $_json_len))');
        p('        (then (if (i32.eq (i32.load8_u (i32.add (global.get $_json_data) (global.get $_json_pos))) (i32.const 0x2C))');
        p('          (then (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))))))');
        p('      (local.set $idx (i32.add (local.get $idx) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        // Skip closing ]
        p('    (call $_json_skip_ws)');
        p('    (if (i32.lt_u (global.get $_json_pos) (global.get $_json_len))');
        p('      (then (global.set $_json_pos (i32.add (global.get $_json_pos) (i32.const 1)))))');
        p('    (global.set $_json_tag (i32.const 6))');
        p('    (local.get $arr)');
        p('  )');
        p('');
        // ── Parser: entry point ───────────────────────────────────────────────
        p('  ;; json_parse: entry point — sets up globals, calls _json_parse_value');
        p('  (func $json_parse (param $src i32) (result i32)');
        p('    (local $len i32)');
        p('    (local.set $len (i32.load (local.get $src)))');
        p('    (if (i32.eqz (local.get $len)) (then (return (call $json_obj_new (i32.const 0)))))');
        p('    (global.set $_json_data (i32.add (local.get $src) (i32.const 4)))');
        p('    (global.set $_json_pos (i32.const 0))');
        p('    (global.set $_json_len (local.get $len))');
        p('    (call $_json_parse_value)');
        p('  )');
        p('');
        // ── Stringify: escape a FIL string for JSON output ────────────────────
        p('  ;; _json_escape_str: escape special chars in FIL string for JSON.');
        p('  ;; Defensive: if $s is a null/junk pointer, return emptyStr.');
        p('  (func $_json_escape_str (param $s i32) (result i32)');
        p('    (local $len i32) (local $i i32) (local $ch i32)');
        p('    (local $ptr i32) (local $dst i32)');
        p('    (if (i32.eqz (local.get $s))');
        p(`      (then (return (i32.const ${emptyStr}))))`);
        p('    (local.set $len (i32.load (local.get $s)))');
        // Allocate 2x length (worst case every byte needs escaping)
        p('    (local.set $ptr (call $alloc (i32.add (i32.mul (local.get $len) (i32.const 2)) (i32.const 4))))');
        p('    (local.set $dst (i32.const 0))');
        p('    (local.set $i (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $i) (local.get $len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (i32.add (local.get $s) (i32.const 4)) (local.get $i))))');
        // Check chars that need escaping
        p('      (if (i32.eq (local.get $ch) (i32.const 0x22)) (then'); // "
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x22))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $lp)))');
        p('      (if (i32.eq (local.get $ch) (i32.const 0x5C)) (then'); // backslash
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $lp)))');
        p('      (if (i32.eq (local.get $ch) (i32.const 0x0A)) (then'); // newline → \n
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x6E))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $lp)))');
        p('      (if (i32.eq (local.get $ch) (i32.const 0x0D)) (then'); // CR → \r
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x72))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $lp)))');
        p('      (if (i32.eq (local.get $ch) (i32.const 0x09)) (then'); // tab → \t
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x5C))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (i32.const 0x74))');
        p('        (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $lp)))');
        // Normal byte
        p('      (i32.store8 (i32.add (i32.add (local.get $ptr) (i32.const 4)) (local.get $dst)) (local.get $ch))');
        p('      (local.set $dst (i32.add (local.get $dst) (i32.const 1)))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p('    (i32.store (local.get $ptr) (local.get $dst))');
        p('    (local.get $ptr)');
        p('  )');
        p('');
        // ── String-concat coercion for json values ────────────────────────────
        // Like _json_stringify_value but does NOT quote STRING-tagged values.
        // Used by binary `+` when one operand is a string and the other is json:
        // `"prefix: " + obj.field` should produce `prefix: hello`, not
        // `prefix: "hello"`.
        p('  ;; _json_value_to_str: json value → string, unquoted (concat-style)');
        p('  (func $_json_value_to_str (param $val i32) (param $tag i32) (result i32)');
        p('    (local $r i32)');
        p(`    (local.set $r (i32.const ${emptyStr}))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 0)) (then`); // STRING — unquoted
        p('      (local.set $r (local.get $val))))');
        p(`    (if (i32.eq (local.get $tag) (i32.const 1)) (then`); // NUMBER
        p('      (local.set $r (local.get $val))))');
        p(`    (if (i32.eq (local.get $tag) (i32.const 2)) (then`); // TRUE
        p(`      (local.set $r (i32.const ${sTrue}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 3)) (then`); // FALSE
        p(`      (local.set $r (i32.const ${sFalse}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 4)) (then`); // NULL
        p(`      (local.set $r (i32.const ${sNull}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 5)) (then`); // OBJECT
        p('      (local.set $r (call $json_stringify (local.get $val)))))');
        p(`    (if (i32.eq (local.get $tag) (i32.const 6)) (then`); // ARRAY
        p('      (local.set $r (call $_json_stringify_array (local.get $val)))))');
        p('    (local.get $r)');
        p('  )');
        p('');
        // ── String → f64 parser ───────────────────────────────────────────────
        // Parses the FIL-string at $str (4-byte length prefix + utf-8 bytes) as
        // a decimal f64. Handles optional leading minus, integer part, and an
        // optional fractional part after `.`. Does NOT handle exponent notation
        // (`1e5`) — JSON allows it but JSON.stringify never emits it for the
        // ranges flow-run cares about. Returns 0.0 on null pointer or empty
        // string. Stops at the first non-digit character that isn't '.' (or
        // ',' / '}' / whitespace etc.), matching the lenient parsing the i32
        // / i64 paths use.
        p('  ;; _str_to_f64: parse a FIL string as a decimal f64.');
        p('  (func $_str_to_f64 (param $str i32) (result f64)');
        p('    (local $len i32) (local $i i32) (local $base i32) (local $ch i32)');
        p('    (local $sign f64) (local $intPart f64) (local $frac f64) (local $div f64)');
        p('    (if (i32.eqz (local.get $str)) (then (return (f64.const 0))))');
        p('    (local.set $len (i32.load (local.get $str)))');
        p('    (local.set $base (i32.add (local.get $str) (i32.const 4)))');
        p('    (if (i32.eqz (local.get $len)) (then (return (f64.const 0))))');
        p('    (local.set $sign (f64.const 1))');
        p('    (local.set $intPart (f64.const 0))');
        p('    (local.set $frac (f64.const 0))');
        p('    (local.set $div (f64.const 1))');
        // Sign
        p('    (local.set $ch (i32.load8_u (local.get $base)))');
        p('    (if (i32.eq (local.get $ch) (i32.const 45)) (then'); // '-'
        p('      (local.set $sign (f64.const -1))');
        p('      (local.set $i (i32.const 1))))');
        // Integer part
        p('    (block $intDone (loop $intLp');
        p('      (br_if $intDone (i32.ge_u (local.get $i) (local.get $len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (local.get $base) (local.get $i))))');
        p('      (if (i32.eq (local.get $ch) (i32.const 46)) (then'); // '.'
        p('        (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('        (br $intDone)))');
        p('      (br_if $intDone (i32.lt_u (local.get $ch) (i32.const 48)))');
        p('      (br_if $intDone (i32.gt_u (local.get $ch) (i32.const 57)))');
        p('      (local.set $intPart');
        p('        (f64.add');
        p('          (f64.mul (local.get $intPart) (f64.const 10))');
        p('          (f64.convert_i32_u (i32.sub (local.get $ch) (i32.const 48)))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $intLp)))');
        // Fractional part (continues from $i, which is past the `.` if there was one)
        p('    (block $fracDone (loop $fracLp');
        p('      (br_if $fracDone (i32.ge_u (local.get $i) (local.get $len)))');
        p('      (local.set $ch (i32.load8_u (i32.add (local.get $base) (local.get $i))))');
        p('      (br_if $fracDone (i32.lt_u (local.get $ch) (i32.const 48)))');
        p('      (br_if $fracDone (i32.gt_u (local.get $ch) (i32.const 57)))');
        p('      (local.set $div (f64.mul (local.get $div) (f64.const 10)))');
        p('      (local.set $frac');
        p('        (f64.add (local.get $frac)');
        p('          (f64.div');
        p('            (f64.convert_i32_u (i32.sub (local.get $ch) (i32.const 48)))');
        p('            (local.get $div))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $fracLp)))');
        p('    (f64.mul (local.get $sign) (f64.add (local.get $intPart) (local.get $frac)))');
        p('  )');
        p('');
        // ── json value → f64 coercion ─────────────────────────────────────────
        // Mirror of $_json_value_to_str but produces an f64. Used by the
        // `const t: f64 = jsonExpr` typed-initializer coercion in
        // emitVariableDeclaration. The NUMBER and STRING tags both decode the
        // string content via $_str_to_f64; bools map to 1.0/0.0; null,
        // object, and array map to 0.0 (matching the lenient i32 path).
        p('  ;; _json_value_to_f64: json value → f64, dispatched on $_json_tag.');
        p('  (func $_json_value_to_f64 (param $val i32) (param $tag i32) (result f64)');
        p('    (local $r f64)');
        p('    (local.set $r (f64.const 0))');
        p('    (if (i32.eq (local.get $tag) (i32.const 0)) (then'); // STRING
        p('      (local.set $r (call $_str_to_f64 (local.get $val)))))');
        p('    (if (i32.eq (local.get $tag) (i32.const 1)) (then'); // NUMBER (val is a string)
        p('      (local.set $r (call $_str_to_f64 (local.get $val)))))');
        p('    (if (i32.eq (local.get $tag) (i32.const 2)) (then'); // TRUE
        p('      (local.set $r (f64.const 1))))');
        // tags 3 (FALSE), 4 (NULL), 5 (OBJECT), 6 (ARRAY) fall through with r = 0.
        p('    (local.get $r)');
        p('  )');
        p('');
        // ── Stringify: value dispatch ─────────────────────────────────────────
        p('  ;; _json_stringify_value: stringify any tagged value');
        p('  (func $_json_stringify_value (param $val i32) (param $tag i32) (result i32)');
        p('    (local $r i32)');
        p(`    (local.set $r (i32.const ${emptyStr}))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 0)) (then`); // STRING
        p(`      (local.set $r (call $str_concat (call $str_concat (i32.const ${sQuote}) (call $_json_escape_str (local.get $val))) (i32.const ${sQuote})))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 1)) (then`); // NUMBER
        p('      (local.set $r (local.get $val))))');
        p(`    (if (i32.eq (local.get $tag) (i32.const 2)) (then`); // TRUE
        p(`      (local.set $r (i32.const ${sTrue}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 3)) (then`); // FALSE
        p(`      (local.set $r (i32.const ${sFalse}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 4)) (then`); // NULL
        p(`      (local.set $r (i32.const ${sNull}))))`);
        p(`    (if (i32.eq (local.get $tag) (i32.const 5)) (then`); // OBJECT
        p('      (local.set $r (call $json_stringify (local.get $val)))))');
        p(`    (if (i32.eq (local.get $tag) (i32.const 6)) (then`); // ARRAY
        p('      (local.set $r (call $_json_stringify_array (local.get $val)))))');
        p('    (local.get $r)');
        p('  )');
        p('');
        // ── Stringify: object ─────────────────────────────────────────────────
        p('  ;; json_stringify: json object table → JSON string');
        p('  (func $json_stringify (param $obj i32) (result i32)');
        p('    (local $count i32) (local $i i32) (local $result i32)');
        p('    (local $base i32) (local $key i32) (local $val i32) (local $tag i32)');
        p('    (local.set $count (i32.load (local.get $obj)))');
        p(`    (if (i32.eqz (local.get $count)) (then (return (i32.const ${sEmptyObj}))))`);
        p(`    (local.set $result (i32.const ${sLBrace}))`);
        p('    (local.set $i (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $i) (local.get $count)))');
        p('      (local.set $base (i32.add (local.get $obj) (i32.add (i32.const 4) (i32.mul (local.get $i) (i32.const 12)))))');
        p('      (local.set $key (i32.load (local.get $base)))');
        p('      (local.set $val (i32.load offset=4 (local.get $base)))');
        p('      (local.set $tag (i32.load offset=8 (local.get $base)))');
        // Comma before all but first
        p('      (if (local.get $i)');
        p(`        (then (local.set $result (call $str_concat (local.get $result) (i32.const ${sComma})))))`);
        // "key":value  (open-quote + key + close-quote + ':' + stringified value)
        p(`      (local.set $result (call $str_concat (local.get $result) (i32.const ${sQuote})))`);
        p('      (local.set $result (call $str_concat (local.get $result) (call $_json_escape_str (local.get $key))))');
        p(`      (local.set $result (call $str_concat (local.get $result) (i32.const ${sCloseKeyColon})))`);
        p('      (local.set $result (call $str_concat (local.get $result) (call $_json_stringify_value (local.get $val) (local.get $tag))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p(`    (call $str_concat (local.get $result) (i32.const ${sRBrace}))`);
        p('  )');
        p('');
        // ── Stringify: array ──────────────────────────────────────────────────
        p('  ;; _json_stringify_array: json array table → JSON string');
        p('  (func $_json_stringify_array (param $arr i32) (result i32)');
        p('    (local $count i32) (local $i i32) (local $result i32)');
        p('    (local $base i32) (local $val i32) (local $tag i32)');
        p('    (local.set $count (i32.load (local.get $arr)))');
        p(`    (if (i32.eqz (local.get $count)) (then (return (i32.const ${sEmptyArr}))))`);
        p(`    (local.set $result (i32.const ${sLBracket}))`);
        p('    (local.set $i (i32.const 0))');
        p('    (block $done (loop $lp');
        p('      (br_if $done (i32.ge_u (local.get $i) (local.get $count)))');
        p('      (local.set $base (i32.add (local.get $arr) (i32.add (i32.const 4) (i32.mul (local.get $i) (i32.const 8)))))');
        p('      (local.set $val (i32.load (local.get $base)))');
        p('      (local.set $tag (i32.load offset=4 (local.get $base)))');
        p('      (if (local.get $i)');
        p(`        (then (local.set $result (call $str_concat (local.get $result) (i32.const ${sComma})))))`);
        p('      (local.set $result (call $str_concat (local.get $result) (call $_json_stringify_value (local.get $val) (local.get $tag))))');
        p('      (local.set $i (i32.add (local.get $i) (i32.const 1)))');
        p('      (br $lp)');
        p('    ))');
        p(`    (call $str_concat (local.get $result) (i32.const ${sRBracket}))`);
        p('  )');
        p('');
        return L;
    }
    // ─── Function emission (sync and WASI-async alike) ──────────────────────────
    emitSyncFunction(fn, isAsync = false) {
        const ctx = {
            fnName: fn.name,
            isAsync,
            fnReturnType: fn.returnType,
            localVars: new Map(),
            loopLabels: [],
            labelMap: new Map(),
            tmpCount: 0,
            lines: [],
            indent: 1,
        };
        // Build param list
        const params = fn.params.map(p => `(param $${p.name} ${this.watType(p.type)})`).join(' ');
        const watRet = this.watType(fn.returnType);
        const retType = (watRet === 'void' || watRet === '') ? '' : ` (result ${watRet})`;
        ctx.lines.push(`  (func $${fn.name} ${params}${retType}`);
        // Register params in scope
        for (const p of fn.params) {
            ctx.localVars.set(p.name, p.type);
        }
        // Collect local variables first (forward scan)
        const locals = this.collectLocals(fn.body, ctx);
        for (const [name, type] of locals) {
            ctx.lines.push(`    (local $${name} ${this.watType(type)})`);
            ctx.localVars.set(name, type);
        }
        // Emit body. The last statement is marked as tail-position when the
        // function has a non-void return type, so an `if`/`else` there can be
        // emitted with a matching `(result <T>)` clause and the WAT validator
        // sees a consistent stack at implicit return.
        ctx.indent = 2;
        const bodyStmts = fn.body.body;
        const watRetForLoop = this.watType(fn.returnType);
        const fnHasResult = watRetForLoop !== 'void' && watRetForLoop !== '';
        for (let i = 0; i < bodyStmts.length; i++) {
            const isLast = i === bodyStmts.length - 1;
            this.emitStatement(bodyStmts[i], ctx, fnHasResult && isLast);
        }
        // If void function and no return, add nothing
        if (fn.returnType === 'void' || fn.returnType === 'unknown') {
            // no-op
        }
        ctx.lines.push('  )');
        return ctx.lines;
    }
    // ─── Async init function ─────────────────────────────────────────────────
    emitAsyncInit(info) {
        const lines = [];
        const layout = info.frameLayout;
        lines.push(`  ;; Async init: ${info.funcName}`);
        lines.push(`  (func $init_${info.funcName} (result i32)`);
        lines.push('    (local $fp i32)');
        lines.push(`    (local.set $fp (call $alloc (i32.const ${layout.frameSize})))`);
        // Zero the frame
        lines.push(`    (memory.fill (local.get $fp) (i32.const 0) (i32.const ${layout.frameSize}))`);
        // Set func_idx
        lines.push(`    (i32.store offset=${await_lifter_1.FRAME_HEADER.func_idx} (local.get $fp) (i32.const ${info.funcIdx}))`);
        // Set catch_state and finally_state to -1
        lines.push(`    (i32.store offset=${await_lifter_1.FRAME_HEADER.catch_state} (local.get $fp) (i32.const -1))`);
        lines.push(`    (i32.store offset=${await_lifter_1.FRAME_HEADER.finally_state} (local.get $fp) (i32.const -1))`);
        lines.push('    (local.get $fp)');
        lines.push('  )');
        return lines;
    }
    // ─── Async resume function ─────────────────────────────────────────────────
    emitAsyncResume(info, fn) {
        const ctx = {
            fnName: fn.name,
            isAsync: true,
            // Carry the function's declared return type even though the
            // state-machine transform routes if/return through state-jump tables
            // rather than structured `if` blocks — keeps the context shape
            // consistent and is harmless when tailReturn is never set true.
            fnReturnType: fn.returnType,
            smInfo: info,
            localVars: new Map(),
            loopLabels: [],
            labelMap: new Map(),
            tmpCount: 0,
            lines: [],
            indent: 1,
        };
        // Register frame slots as available variables
        for (const slot of info.frameLayout.slots) {
            ctx.localVars.set(slot.name, slot.type);
        }
        const numStates = info.numStates;
        ctx.lines.push(`  ;; Async resume: ${fn.name} (${numStates} states)`);
        ctx.lines.push(`  (func $resume_${fn.name} (param $fp i32) (result i32)`);
        ctx.lines.push('    (local $state i32)');
        ctx.lines.push(`    (local.set $state (i32.load offset=${await_lifter_1.FRAME_HEADER.state} (local.get $fp)))`);
        ctx.lines.push('');
        // Collect locals for temporaries
        const locals = this.collectLocals(fn.body, ctx);
        for (const [name, type] of locals) {
            // Only add locals that aren't in the frame
            if (!info.frameLayout.slots.find(s => s.name === name)) {
                ctx.lines.push(`    (local $tmp_${name} ${this.watType(type)})`);
            }
        }
        // Nested blocks for br_table dispatch
        // (block $end (block $sN ... (block $s1 (block $s0
        //   (br_table $s0 $s1 ... $sN $end (local.get $state))
        // )
        // ...each state...
        // ))
        ctx.lines.push('    (block $blk_end');
        for (let s = numStates - 1; s >= 0; s--) {
            ctx.lines.push(`    (block $blk_s${s}`);
        }
        // br_table
        const stateLabels = Array.from({ length: numStates }, (_, i) => `$blk_s${i}`).join(' ');
        ctx.lines.push(`      (br_table ${stateLabels} $blk_end (local.get $state))`);
        // Close $blk_s0 block and emit state 0
        ctx.lines.push('    )'); // close $blk_s0
        // Emit states
        // We use a simplified approach: generate states from the function body
        // by walking the statements and splitting at await points
        this.emitStateMachineBody(fn, info, ctx);
        // Close remaining state blocks
        for (let s = 1; s < numStates; s++) {
            ctx.lines.push('    )'); // close $blk_s{s}
        }
        ctx.lines.push('    )'); // close $blk_end
        ctx.lines.push('    (i32.const 1)'); // COMPLETE
        ctx.lines.push('  )');
        return ctx.lines;
    }
    // ─── State machine body generation ──────────────────────────────────────────
    emitStateMachineBody(fn, info, ctx) {
        // Number each await expression in order
        let awaitCounter = 0;
        const stateStmts = [[]];
        // Walk statements and split at await expressions
        // This is a simplified linear split (no control flow crossing awaits)
        for (const stmt of fn.body.body) {
            const awaitCount = this.countAwaitExprs(stmt);
            if (awaitCount === 0) {
                stateStmts[stateStmts.length - 1].push(stmt);
            }
            else {
                // Statements with awaits go into their own "group"
                // For simplicity: push to current state, subsequent states start after
                stateStmts[stateStmts.length - 1].push(stmt);
                for (let i = 0; i < awaitCount; i++) {
                    stateStmts.push([]);
                }
            }
        }
        // Emit each state
        for (let s = 0; s < info.numStates; s++) {
            ctx.lines.push(`      ;; === STATE ${s} ===`);
            const stmts = stateStmts[s] ?? [];
            const stateCtx = {
                ...ctx,
                loopLabels: [...ctx.loopLabels],
                labelMap: new Map(ctx.labelMap),
                lines: ctx.lines,
            };
            stateCtx.indent = 3;
            for (const stmt of stmts) {
                this.emitStatementAsync(stmt, stateCtx, info, { counter: awaitCounter });
            }
            if (s < info.numStates - 1) {
                // After each non-final state: should have been handled by await emission
                // If state is complete (all stmts done), fall through to end
                // The await expressions within statements handle returning PENDING
            }
            else {
                // Final state: complete
                ctx.lines.push(`      (i32.store offset=${await_lifter_1.FRAME_HEADER.state} (local.get $fp) (i32.const ${s}))`);
                // Return COMPLETE
                ctx.lines.push('      (return (i32.const 1))');
            }
            // Close state block and open next
            if (s < info.numStates - 1) {
                // don't need extra close here since we already close blocks above
            }
        }
    }
    countAwaitExprs(node) {
        let count = 0;
        const walk = (n) => {
            if (n.kind === 'AwaitExpression') {
                count++;
                return; // don't recurse into nested awaits
            }
            this.walkNodeShallow(n, walk);
        };
        walk(node);
        return count;
    }
    walkNodeShallow(node, fn) {
        switch (node.kind) {
            case 'BlockStatement':
                for (const s of node.body)
                    fn(s);
                break;
            case 'VariableDeclaration':
                if (node.initializer)
                    fn(node.initializer);
                break;
            case 'ExpressionStatement':
                fn(node.expression);
                break;
            case 'ReturnStatement':
                if (node.argument)
                    fn(node.argument);
                break;
            case 'ThrowStatement':
                fn(node.argument);
                break;
            case 'IfStatement': {
                const i = node;
                fn(i.test);
                fn(i.consequent);
                if (i.alternate)
                    fn(i.alternate);
                break;
            }
            case 'WhileStatement': {
                const w = node;
                fn(w.test);
                fn(w.body);
                break;
            }
            case 'ForStatement': {
                const f = node;
                if (f.init)
                    fn(f.init);
                if (f.test)
                    fn(f.test);
                if (f.update)
                    fn(f.update);
                fn(f.body);
                break;
            }
            case 'ForOfStatement': {
                const f = node;
                fn(f.iterable);
                fn(f.body);
                break;
            }
            case 'ForInStatement': {
                const f = node;
                fn(f.object);
                fn(f.body);
                break;
            }
            case 'SwitchStatement': {
                const s = node;
                fn(s.discriminant);
                for (const c of s.cases) {
                    if (c.test)
                        fn(c.test);
                    for (const st of c.consequent)
                        fn(st);
                }
                break;
            }
            case 'TryStatement': {
                const t = node;
                fn(t.block);
                if (t.handler)
                    fn(t.handler.body);
                if (t.finalizer)
                    fn(t.finalizer);
                break;
            }
            case 'LabeledStatement':
                fn(node.body);
                break;
            case 'BinaryExpression': {
                const b = node;
                fn(b.left);
                fn(b.right);
                break;
            }
            case 'LogicalExpression': {
                const l = node;
                fn(l.left);
                fn(l.right);
                break;
            }
            case 'UnaryExpression':
                fn(node.argument);
                break;
            case 'UpdateExpression':
                fn(node.argument);
                break;
            case 'ConditionalExpression': {
                const c = node;
                fn(c.test);
                fn(c.consequent);
                fn(c.alternate);
                break;
            }
            case 'AssignmentExpression': {
                const a = node;
                fn(a.left);
                fn(a.right);
                break;
            }
            case 'CallExpression': {
                const c = node;
                fn(c.callee);
                for (const arg of c.arguments)
                    fn(arg);
                break;
            }
            case 'MemberExpression': {
                const m = node;
                fn(m.object);
                if (m.computed)
                    fn(m.property);
                break;
            }
            case 'AwaitExpression':
                // Do NOT recurse into AwaitExpression argument - counted separately
                break;
            case 'NewExpression': {
                const n = node;
                for (const a of n.arguments)
                    fn(a);
                break;
            }
            case 'TypeAssertion':
                fn(node.expression);
                break;
            case 'TemplateLiteral': {
                const t = node;
                for (const e of t.expressions)
                    fn(e);
                break;
            }
            case 'ArrayExpression': {
                const a = node;
                for (const e of a.elements)
                    fn(e);
                break;
            }
            case 'ObjectExpression': {
                const o = node;
                for (const p of o.properties)
                    fn(p.value);
                break;
            }
            case 'SpreadElement':
                fn(node.argument);
                break;
            case 'SequenceExpression': {
                const s = node;
                for (const e of s.expressions)
                    fn(e);
                break;
            }
        }
    }
    // ─── Async statement emission ─────────────────────────────────────────────
    emitStatementAsync(stmt, ctx, info, awaitState) {
        // For the async resume function, we emit statements but handle awaits specially
        // We delegate to the regular statement emitter which will handle awaits via emitExprAsync
        this.emitStatement(stmt, ctx);
    }
    // ─── Statement emission (sync and async) ─────────────────────────────────
    /**
     * Whether `stmt`'s execution unconditionally exits the enclosing function
     * (via return-with-value, throw, or an exhaustive if/else where both arms
     * exit). Used by emitStatement to decide when an `if` in tail position
     * needs a `(result <T>)` clause: WebAssembly validates blocks by declared
     * signature, so we only widen the if's result type when we know all arm
     * paths produce a value (or are unreachable via throw/return).
     */
    isValueProducingTail(stmt) {
        switch (stmt.kind) {
            case 'ReturnStatement':
                return stmt.argument != null;
            case 'ThrowStatement':
                // emitThrow ends the arm with `(unreachable)`, which polymorphically
                // subsumes any declared result type — so a throw-terminated arm is
                // safe to pair with a return-terminated arm under `(if (result T))`.
                return true;
            case 'BlockStatement':
                return stmt.body.length > 0
                    && this.isValueProducingTail(stmt.body[stmt.body.length - 1]);
            case 'IfStatement':
                return stmt.alternate != null
                    && this.isValueProducingTail(stmt.consequent)
                    && this.isValueProducingTail(stmt.alternate);
            default:
                return false;
        }
    }
    emitStatement(stmt, ctx, tailReturn = false) {
        const ind = '  '.repeat(ctx.indent);
        switch (stmt.kind) {
            case 'BlockStatement':
                // Propagate tailReturn only to the structurally-last statement of
                // the block — earlier statements aren't in tail position.
                for (let i = 0; i < stmt.body.length; i++) {
                    const isLast = i === stmt.body.length - 1;
                    this.emitStatement(stmt.body[i], ctx, tailReturn && isLast);
                }
                break;
            case 'VariableDeclaration': {
                if (stmt.initializer) {
                    const initLines = this.emitExpr(stmt.initializer, ctx);
                    const local = this.getLocalName(stmt.name, ctx);
                    ctx.lines.push(...initLines.map(l => ind + l));
                    // Implicit string coercion on typed declarations: `const s: string
                    // = jsonExpr` (or i32/f64/etc.) runs the same runtime conversion
                    // `"" + jsonExpr` would. Lets users skip the empty-string-concat
                    // workaround for the common json-field-to-string case.
                    if (stmt.typeAnnotation === 'string') {
                        const initType = stmt.initializer.filType;
                        if (initType && initType !== 'string' && initType !== 'unknown') {
                            const coercion = this.coerceToString(initType);
                            if (coercion)
                                ctx.lines.push(`${ind}${coercion}`);
                        }
                    }
                    // Implicit f64 coercion when the initializer is json: `const t: f64
                    // = parsed.temperature` would otherwise try to `local.set` an i32
                    // pointer into an f64 slot and fail wabt validation. The runtime
                    // helper reads $_json_tag (set by the preceding json op) and
                    // decodes the value's numeric content; tags that don't carry a
                    // number return 0.0. Mirrors the existing string coercion above.
                    if (stmt.typeAnnotation === 'f64') {
                        const initType = stmt.initializer.filType;
                        if (initType === 'json') {
                            ctx.lines.push(`${ind}(global.get $_json_tag)`);
                            ctx.lines.push(`${ind}(call $_json_value_to_f64)`);
                        }
                    }
                    ctx.lines.push(`${ind}(local.set ${local})`);
                }
                break;
            }
            case 'ExpressionStatement': {
                const exprLines = this.emitExpr(stmt.expression, ctx);
                ctx.lines.push(...exprLines.map(l => ind + l));
                // Drop the result if any (expressions as statements).
                //
                // Stack effect by expression kind:
                //   • AssignmentExpression (`x = y`): emitAssignment re-loads the
                //     target after the store, so it pushes 1 value → must drop.
                //   • UpdateExpression (`x++`, `++x`): emitUpdateExpression stores
                //     and returns; it does NOT push the post-store value → no drop.
                //   • Everything else with a non-void type pushes 1 value → drop.
                const exprType = stmt.expression.filType ?? 'void';
                const pushesValue = stmt.expression.kind !== 'UpdateExpression';
                if (exprType !== 'void' && exprType !== 'unknown' && pushesValue) {
                    ctx.lines.push(`${ind}(drop)`);
                }
                break;
            }
            case 'ReturnStatement': {
                if (stmt.argument) {
                    const argLines = this.emitExpr(stmt.argument, ctx);
                    ctx.lines.push(...argLines.map(l => ind + l));
                    ctx.lines.push(`${ind}(return)`);
                }
                else {
                    ctx.lines.push(`${ind}(return)`);
                }
                break;
            }
            case 'ThrowStatement': {
                // Simplified: call proc_exit(1) to signal error
                const argLines = this.emitExpr(stmt.argument, ctx);
                ctx.lines.push(...argLines.map(l => ind + l));
                ctx.lines.push(`${ind}(drop)`);
                ctx.lines.push(`${ind}(call $wasi_proc_exit (i32.const 1))`);
                ctx.lines.push(`${ind}(unreachable)`);
                break;
            }
            case 'IfStatement': {
                const testLines = this.emitExpr(stmt.test, ctx);
                ctx.lines.push(...testLines.map(l => ind + l));
                // Tail-return widening: when this if is in tail position of a
                // non-void function and both arms unconditionally produce a value
                // (return-with-arg, throw, or nested exhaustive if), declare the
                // if's result type to match the function's. Without this clause
                // the if's signature is `[i32]→[]` and validation fails at the
                // function's implicit return with `expected [T] but got []`. The
                // inner `(return)` instructions stay — they still consume the
                // pushed value and mark the arm unreachable, which polymorphically
                // subsumes the declared `[T]` arm-end stack.
                const watRet = ctx.fnReturnType !== undefined
                    ? this.watType(ctx.fnReturnType)
                    : '';
                const fnHasResult = watRet !== '' && watRet !== 'void';
                const wantResult = tailReturn &&
                    fnHasResult &&
                    stmt.alternate != null &&
                    this.isValueProducingTail(stmt.consequent) &&
                    this.isValueProducingTail(stmt.alternate);
                const resultClause = wantResult ? ` (result ${watRet})` : '';
                if (stmt.alternate) {
                    ctx.lines.push(`${ind}(if${resultClause}`);
                    ctx.lines.push(`${ind}  (then`);
                    const thenCtx = { ...ctx, indent: ctx.indent + 2 };
                    this.emitStatement(stmt.consequent, thenCtx, wantResult);
                    ctx.lines.push(`${ind}  )`);
                    ctx.lines.push(`${ind}  (else`);
                    const elseCtx = { ...ctx, indent: ctx.indent + 2 };
                    this.emitStatement(stmt.alternate, elseCtx, wantResult);
                    ctx.lines.push(`${ind}  )`);
                    ctx.lines.push(`${ind})`);
                }
                else {
                    // Single-arm if can never be value-producing (no else branch),
                    // so the result-clause logic above never triggers here.
                    ctx.lines.push(`${ind}(if`);
                    ctx.lines.push(`${ind}  (then`);
                    const thenCtx = { ...ctx, indent: ctx.indent + 2 };
                    this.emitStatement(stmt.consequent, thenCtx);
                    ctx.lines.push(`${ind}  )`);
                    ctx.lines.push(`${ind})`);
                }
                break;
            }
            case 'WhileStatement': {
                const brkLabel = `$brk_${ctx.tmpCount}`;
                const contLabel = `$cont_${ctx.tmpCount}`;
                ctx.tmpCount++;
                ctx.loopLabels.push({ brk: brkLabel, cont: contLabel });
                ctx.lines.push(`${ind}(block ${brkLabel}`);
                ctx.lines.push(`${ind}  (loop ${contLabel}`);
                // Test
                const testLines = this.emitExpr(stmt.test, ctx);
                ctx.lines.push(...testLines.map(l => ind + '  ' + l));
                ctx.lines.push(`${ind}  (i32.eqz)`);
                ctx.lines.push(`${ind}  (br_if ${brkLabel})`);
                const bodyCtx = { ...ctx, indent: ctx.indent + 2 };
                this.emitStatement(stmt.body, bodyCtx);
                ctx.lines.push(`${ind}  (br ${contLabel})`);
                ctx.lines.push(`${ind}  )`);
                ctx.lines.push(`${ind})`);
                ctx.loopLabels.pop();
                break;
            }
            case 'ForStatement': {
                const brkLabel = `$brk_${ctx.tmpCount}`;
                const contLabel = `$cont_${ctx.tmpCount}`;
                ctx.tmpCount++;
                if (stmt.init) {
                    this.emitStatement(stmt.init, ctx);
                }
                ctx.loopLabels.push({ brk: brkLabel, cont: contLabel });
                ctx.lines.push(`${ind}(block ${brkLabel}`);
                ctx.lines.push(`${ind}  (loop ${contLabel}`);
                if (stmt.test) {
                    const testLines = this.emitExpr(stmt.test, ctx);
                    ctx.lines.push(...testLines.map(l => ind + '  ' + l));
                    ctx.lines.push(`${ind}  (i32.eqz)`);
                    ctx.lines.push(`${ind}  (br_if ${brkLabel})`);
                }
                const bodyCtx = { ...ctx, indent: ctx.indent + 2 };
                this.emitStatement(stmt.body, bodyCtx);
                if (stmt.update) {
                    const updateLines = this.emitExpr(stmt.update, ctx);
                    ctx.lines.push(...updateLines.map(l => ind + '  ' + l));
                    const updateType = stmt.update.filType ?? 'void';
                    if (updateType !== 'void') {
                        ctx.lines.push(`${ind}  (drop)`);
                    }
                }
                ctx.lines.push(`${ind}  (br ${contLabel})`);
                ctx.lines.push(`${ind}  )`);
                ctx.lines.push(`${ind})`);
                ctx.loopLabels.pop();
                break;
            }
            case 'ForOfStatement': {
                // for (const item of iterable) { body }
                // Iterable is treated as a json array (the typical case from JSON.parse).
                // Locals (__forof_iter_N, __forof_idx_N, __forof_len_N, plus the user
                // binding `item`) are pre-declared by collectLocals at the same slot.
                const slot = ctx.tmpCount;
                ctx.tmpCount++;
                const iterVar = `$__forof_iter_${slot}`;
                const idxVar = `$__forof_idx_${slot}`;
                const lenVar = `$__forof_len_${slot}`;
                const brkLabel = `$brk_${slot}`;
                const contLabel = `$cont_${slot}`;
                const iterLines = this.emitExpr(stmt.iterable, ctx);
                ctx.lines.push(...iterLines.map(l => ind + l));
                ctx.lines.push(`${ind}(local.set ${iterVar})`);
                ctx.lines.push(`${ind}(local.set ${lenVar} (call $json_array_len (local.get ${iterVar})))`);
                ctx.lines.push(`${ind}(local.set ${idxVar} (i32.const 0))`);
                ctx.loopLabels.push({ brk: brkLabel, cont: contLabel });
                ctx.lines.push(`${ind}(block ${brkLabel}`);
                ctx.lines.push(`${ind}  (loop ${contLabel}`);
                // Exit when idx >= len
                ctx.lines.push(`${ind}    (br_if ${brkLabel} (i32.ge_s (local.get ${idxVar}) (local.get ${lenVar})))`);
                // item = json_array_get(iter, idx)
                const itemLocal = this.getLocalName(stmt.item, ctx);
                ctx.lines.push(`${ind}    (local.set ${itemLocal} (call $json_array_get (local.get ${iterVar}) (local.get ${idxVar})))`);
                const bodyCtx = { ...ctx, indent: ctx.indent + 2 };
                this.emitStatement(stmt.body, bodyCtx);
                ctx.lines.push(`${ind}    (local.set ${idxVar} (i32.add (local.get ${idxVar}) (i32.const 1)))`);
                ctx.lines.push(`${ind}    (br ${contLabel})`);
                ctx.lines.push(`${ind}  )`);
                ctx.lines.push(`${ind})`);
                ctx.loopLabels.pop();
                break;
            }
            case 'ForInStatement': {
                ctx.lines.push(`${ind};; for..in loop (simplified)`);
                break;
            }
            case 'SwitchStatement': {
                const discLines = this.emitExpr(stmt.discriminant, ctx);
                const discType = stmt.discriminant.filType ?? 'i32';
                const discVar = `$__disc_${ctx.tmpCount}`;
                ctx.tmpCount++;
                ctx.lines.push(...discLines.map(l => ind + l));
                ctx.lines.push(`${ind};; switch statement (simplified as if-else chain)`);
                // Emit as if-else chain
                let first = true;
                for (const cas of stmt.cases) {
                    if (!cas.test)
                        continue; // default handled at end
                    ctx.lines.push(`${ind}(block $sw_${ctx.tmpCount}`);
                    // Compare
                    const caseLines = this.emitExpr(cas.test, ctx);
                    ctx.lines.push(...discLines.map(l => ind + '  ' + l));
                    ctx.lines.push(...caseLines.map(l => ind + '  ' + l));
                    ctx.lines.push(`${ind}  (i32.ne)`);
                    ctx.lines.push(`${ind}  (br_if $sw_${ctx.tmpCount})`);
                    const caseCtx = { ...ctx, indent: ctx.indent + 1 };
                    for (const s of cas.consequent)
                        this.emitStatement(s, caseCtx);
                    ctx.lines.push(`${ind})`);
                    ctx.tmpCount++;
                }
                // Default case
                const defCase = stmt.cases.find(c => !c.test);
                if (defCase) {
                    const defCtx = { ...ctx };
                    for (const s of defCase.consequent)
                        this.emitStatement(s, defCtx);
                }
                break;
            }
            case 'TryStatement': {
                // Simplified try/catch: just emit the block, catch is a fallback
                ctx.lines.push(`${ind};; try block`);
                for (const s of stmt.block.body)
                    this.emitStatement(s, ctx);
                if (stmt.handler) {
                    ctx.lines.push(`${ind};; catch (${stmt.handler.param ?? '_'}) - simplified`);
                    // In real impl we'd check error_code here
                }
                if (stmt.finalizer) {
                    ctx.lines.push(`${ind};; finally`);
                    for (const s of stmt.finalizer.body)
                        this.emitStatement(s, ctx);
                }
                break;
            }
            case 'BreakStatement': {
                if (ctx.loopLabels.length > 0) {
                    ctx.lines.push(`${ind}(br ${ctx.loopLabels[ctx.loopLabels.length - 1].brk})`);
                }
                break;
            }
            case 'ContinueStatement': {
                if (ctx.loopLabels.length > 0) {
                    ctx.lines.push(`${ind}(br ${ctx.loopLabels[ctx.loopLabels.length - 1].cont})`);
                }
                break;
            }
            case 'LabeledStatement': {
                this.emitStatement(stmt.body, ctx);
                break;
            }
        }
    }
    // ─── Expression emission (returns array of WAT lines) ────────────────────
    emitExpr(expr, ctx) {
        switch (expr.kind) {
            case 'Literal':
                return this.emitLiteral(expr, ctx);
            case 'Identifier':
                return this.emitIdentifier(expr, ctx);
            case 'TemplateLiteral':
                return this.emitTemplateLiteral(expr, ctx);
            case 'ArrayExpression':
                return this.emitArrayExpression(expr, ctx);
            case 'ObjectExpression':
                return this.emitObjectExpression(expr, ctx);
            case 'BinaryExpression':
                return this.emitBinaryExpression(expr, ctx);
            case 'LogicalExpression':
                return this.emitLogicalExpression(expr, ctx);
            case 'UnaryExpression':
                return this.emitUnaryExpression(expr, ctx);
            case 'UpdateExpression':
                return this.emitUpdateExpression(expr, ctx);
            case 'ConditionalExpression':
                return this.emitConditionalExpression(expr, ctx);
            case 'AssignmentExpression':
                return this.emitAssignment(expr, ctx);
            case 'CallExpression':
                return this.emitCallExpression(expr, ctx);
            case 'MemberExpression':
                return this.emitMemberExpression(expr, ctx);
            case 'AwaitExpression':
                return this.emitAwaitExpression(expr, ctx);
            case 'NewExpression':
                return this.emitNewExpression(expr, ctx);
            case 'TypeAssertion':
                return this.emitExpr(expr.expression, ctx);
            case 'SpreadElement':
                return this.emitExpr(expr.argument, ctx);
            case 'SequenceExpression': {
                const lines = [];
                for (let i = 0; i < expr.expressions.length; i++) {
                    lines.push(...this.emitExpr(expr.expressions[i], ctx));
                    if (i < expr.expressions.length - 1)
                        lines.push('(drop)');
                }
                return lines;
            }
            default:
                return [`(i32.const 0) ;; unsupported expr: ${expr.kind}`];
        }
    }
    emitLiteral(expr, ctx) {
        if (typeof expr.value === 'boolean') {
            return [`(i32.const ${expr.value ? 1 : 0})`];
        }
        if (typeof expr.value === 'number') {
            // Prefer the typechecker-assigned filType when present (contextual
            // typing handles `let a: f64 = 0` correctly). Fall back to the
            // value-shape heuristic only when we have no type info.
            if (expr.filType === 'i64')
                return [`(i64.const ${expr.value})`];
            if (expr.filType === 'f64')
                return [`(f64.const ${expr.value})`];
            if (expr.filType === 'i32')
                return [`(i32.const ${expr.value})`];
            // No filType set — use the literal's value shape.
            if (Number.isInteger(expr.value)) {
                return [`(i32.const ${expr.value})`];
            }
            return [`(f64.const ${expr.value})`];
        }
        if (typeof expr.value === 'string') {
            const addr = this.internString(expr.value);
            return [`(i32.const ${addr})`];
        }
        // null/undefined
        return ['(i32.const 0)'];
    }
    emitIdentifier(expr, ctx) {
        const name = expr.name;
        // Check if it's a local variable
        if (ctx.localVars.has(name)) {
            return [this.getLocalRef(name, ctx)];
        }
        // Check built-in constants
        if (name === 'undefined' || name === 'null')
            return ['(i32.const 0)'];
        if (name === 'true')
            return ['(i32.const 1)'];
        if (name === 'false')
            return ['(i32.const 0)'];
        // Unknown identifier — most commonly v1 `=js:` runtime names like `$vars`
        // that survived into a sync helper body. Emit the address of the
        // pre-interned empty string (which has a valid 4-byte length=0 prefix) so
        // downstream `$json_get_field` / `$_json_escape_str` / `$str_concat`
        // operate on a well-formed empty value instead of dereferencing memory
        // at address 0. Without this guard, `(i32.const 0)` would cause downstream
        // helpers to load "count"/"length" from byte 0 of the WASM linear memory —
        // which is unallocated and unpredictable across runtimes.
        const emptyStrAddr = this.emptyStringAddr();
        return [`(i32.const ${emptyStrAddr}) ;; unknown: ${name}`];
    }
    emitTemplateLiteral(expr, ctx) {
        // Build a string from parts
        if (expr.quasis.length === 1 && expr.expressions.length === 0) {
            const addr = this.internString(expr.quasis[0]);
            return [`(i32.const ${addr})`];
        }
        const lines = [];
        let first = true;
        for (let i = 0; i < expr.quasis.length; i++) {
            const quasi = expr.quasis[i];
            if (i > 0 && i - 1 < expr.expressions.length) {
                // Add expression part
                const exprLines = this.emitExpr(expr.expressions[i - 1], ctx);
                const exprType = expr.expressions[i - 1].filType ?? 'string';
                if (first) {
                    lines.push(...exprLines);
                    first = false;
                }
                else {
                    lines.push(...exprLines);
                    lines.push('(call $str_concat)');
                }
            }
            if (quasi !== '') {
                const addr = this.internString(quasi);
                if (first) {
                    lines.push(`(i32.const ${addr})`);
                    first = false;
                }
                else {
                    lines.push(`(i32.const ${addr})`);
                    lines.push('(call $str_concat)');
                }
            }
        }
        if (first) {
            lines.push(`(i32.const ${this.internString('')})`);
        }
        return lines;
    }
    emitArrayExpression(expr, ctx) {
        const lines = [];
        const count = expr.elements.length;
        if (count === 0) {
            lines.push('(global.set $_json_tag (i32.const 6))');
            lines.push('(call $json_array_new (i32.const 0))');
            return lines;
        }
        lines.push(`(call $json_array_new (i32.const ${count}))`);
        const tmpName = `__arr_tmp_${ctx.tmpCount++}`;
        lines.push(`(local.set $${tmpName})`);
        for (let i = 0; i < count; i++) {
            const elem = expr.elements[i];
            const elemType = elem.filType ?? 'json';
            let tag = 0;
            const valLines = [];
            if (elem.kind === 'Literal') {
                const lit = elem;
                if (lit.value === true) {
                    tag = 2;
                    valLines.push(`(i32.const ${this.internString('true')})`);
                }
                else if (lit.value === false) {
                    tag = 3;
                    valLines.push(`(i32.const ${this.internString('false')})`);
                }
                else if (lit.value === null) {
                    tag = 4;
                    valLines.push(`(i32.const ${this.emptyStringAddr()})`);
                }
                else if (typeof lit.value === 'number') {
                    tag = 1;
                    valLines.push(...this.emitExpr(elem, ctx));
                    valLines.push(Number.isInteger(lit.value) ? '(call $i32_to_str)' : '(call $f64_to_str)');
                }
                else {
                    tag = 0;
                    valLines.push(...this.emitExpr(elem, ctx));
                }
            }
            else if (elem.kind === 'ObjectExpression') {
                tag = 5;
                valLines.push(...this.emitExpr(elem, ctx));
            }
            else if (elem.kind === 'ArrayExpression') {
                tag = 6;
                valLines.push(...this.emitExpr(elem, ctx));
            }
            else if (elemType === 'i32') {
                tag = 1;
                valLines.push(...this.emitExpr(elem, ctx));
                valLines.push('(call $i32_to_str)');
            }
            else if (elemType === 'i64' || elemType === 'DateTime' || elemType === 'TimeSpan') {
                tag = 1;
                valLines.push(...this.emitExpr(elem, ctx));
                valLines.push('(call $i64_to_str)');
            }
            else if (elemType === 'f64') {
                tag = 1;
                valLines.push(...this.emitExpr(elem, ctx));
                valLines.push('(call $f64_to_str)');
            }
            else if (elemType === 'json') {
                tag = 5;
                valLines.push(...this.emitExpr(elem, ctx));
            }
            else {
                tag = 0;
                valLines.push(...this.emitExpr(elem, ctx));
            }
            lines.push('(call $json_array_set');
            lines.push(`  (local.get $${tmpName})`);
            lines.push(`  (i32.const ${i})`);
            lines.push(...valLines.map(l => '  ' + l));
            lines.push(`  (i32.const ${tag})`);
            lines.push(')');
        }
        lines.push('(global.set $_json_tag (i32.const 6))');
        lines.push(`(local.get $${tmpName})`);
        return lines;
    }
    emitObjectExpression(expr, ctx) {
        const lines = [];
        const count = expr.properties.length;
        if (count === 0) {
            lines.push('(global.set $_json_tag (i32.const 5))');
            lines.push('(call $json_obj_new (i32.const 0))');
            return lines;
        }
        // Allocate object table
        lines.push(`(call $json_obj_new (i32.const ${count}))`);
        const tmpName = `__obj_tmp_${ctx.tmpCount++}`;
        lines.push(`(local.set $${tmpName})`);
        for (let i = 0; i < count; i++) {
            const prop = expr.properties[i];
            const keyAddr = this.internString(prop.key);
            const valExpr = prop.value;
            const valType = valExpr.filType ?? 'json';
            // Determine tag and emit value. For json-typed values whose runtime
            // tag is only knowable at runtime (member access, array index, parse
            // result, bare json identifier), tag is `null` here and we emit
            // `(global.get $_json_tag)` instead of `(i32.const <N>)`. The global
            // is set as a side effect of $json_get_field / $json_array_get /
            // $_json_parse_value / emitObjectExpression / emitArrayExpression;
            // for a bare json identifier the most recent of those wins, which
            // is correct for the common `{ field: obj.path }` pattern but may
            // be stale across intervening json ops — workaround is to hoist
            // into a typed-string local (see SKILL.md).
            let tag = 0; // default: STRING
            const valLines = [];
            if (valExpr.kind === 'Literal') {
                const lit = valExpr;
                if (lit.value === true) {
                    tag = 2;
                    valLines.push(`(i32.const ${this.internString('true')})`);
                }
                else if (lit.value === false) {
                    tag = 3;
                    valLines.push(`(i32.const ${this.internString('false')})`);
                }
                else if (lit.value === null) {
                    tag = 4;
                    valLines.push(`(i32.const ${this.emptyStringAddr()})`);
                }
                else if (typeof lit.value === 'number') {
                    tag = 1;
                    valLines.push(...this.emitExpr(valExpr, ctx));
                    valLines.push(Number.isInteger(lit.value) ? '(call $i32_to_str)' : '(call $f64_to_str)');
                }
                else {
                    // string literal
                    tag = 0;
                    valLines.push(...this.emitExpr(valExpr, ctx));
                }
            }
            else if (valExpr.kind === 'ObjectExpression') {
                tag = 5;
                valLines.push(...this.emitExpr(valExpr, ctx));
            }
            else if (valExpr.kind === 'ArrayExpression') {
                tag = 6;
                valLines.push(...this.emitExpr(valExpr, ctx));
            }
            else if (valType === 'i32') {
                tag = 1;
                valLines.push(...this.emitExpr(valExpr, ctx));
                valLines.push('(call $i32_to_str)');
            }
            else if (valType === 'i64' || valType === 'DateTime' || valType === 'TimeSpan') {
                tag = 1;
                valLines.push(...this.emitExpr(valExpr, ctx));
                valLines.push('(call $i64_to_str)');
            }
            else if (valType === 'f64') {
                tag = 1;
                valLines.push(...this.emitExpr(valExpr, ctx));
                valLines.push('(call $f64_to_str)');
            }
            else if (valType === 'json') {
                // Dynamic tag: read $_json_tag at runtime. The hardcoded `5`
                // (OBJECT) here used to crash with `RuntimeError: memory access
                // out of bounds` when the json value's actual tag was STRING /
                // NUMBER / NULL — $_json_stringify_value walked an object table
                // through what was really a string pointer. See
                // fil/known-issues.md "Object literal with json (or non-string
                // typed) value crashes at runtime".
                tag = null;
                valLines.push(...this.emitExpr(valExpr, ctx));
            }
            else {
                // string or other
                tag = 0;
                valLines.push(...this.emitExpr(valExpr, ctx));
            }
            lines.push(`(call $json_obj_set`);
            lines.push(`  (local.get $${tmpName})`);
            lines.push(`  (i32.const ${i})`);
            lines.push(`  (i32.const ${keyAddr})`);
            lines.push(...valLines.map(l => '  ' + l));
            if (tag === null) {
                lines.push(`  (global.get $_json_tag)`);
            }
            else {
                lines.push(`  (i32.const ${tag})`);
            }
            lines.push(')');
        }
        lines.push('(global.set $_json_tag (i32.const 5))');
        lines.push(`(local.get $${tmpName})`);
        return lines;
    }
    /**
     * Emit the WAT instruction(s) to convert the value on top of the operand
     * stack into a FIL string pointer. Used when one side of a binary `+` is
     * `string` and the other is a numeric / json value — without this, the
     * raw numeric is passed to `$str_concat` as a pointer and crashes (OOB
     * on any nonzero value).
     *
     * For `json` we reuse the tag-tracking global `$_json_tag` that's set by
     * `$json_get_field` / `$_json_parse_value` and dispatch via
     * `$_json_value_to_str`. That helper is like `$_json_stringify_value` but
     * does NOT quote STRING-tagged values (we want `"foo"` to concat as `foo`,
     * not `"foo"`).
     */
    coerceToString(type) {
        if (type === 'i32' || type === 'bool')
            return '(call $i32_to_str)';
        if (type === 'f64')
            return '(call $f64_to_str)';
        if (type === 'i64' || type === 'DateTime' || type === 'TimeSpan') {
            return '(call $i64_to_str)';
        }
        if (type === 'json') {
            // The value is already on the stack; pull the tag from the global the
            // last $json_get_field / $_json_parse_value set, then dispatch.
            return '(global.get $_json_tag) (call $_json_value_to_str)';
        }
        // Unknown / object / array — best-effort: treat the value as already a
        // string pointer. $str_concat's null-substitution guard handles the
        // pathological cases.
        return '';
    }
    emitBinaryExpression(expr, ctx) {
        const lines = [];
        const leftType = expr.left.filType ?? 'i32';
        const rightType = expr.right.filType ?? 'i32';
        const resultType = expr.filType ?? 'i32';
        const op = expr.operator;
        const isFloat = leftType === 'f64' || rightType === 'f64';
        const isLong = leftType === 'i64' || rightType === 'i64' ||
            leftType === 'DateTime' || rightType === 'DateTime' ||
            leftType === 'TimeSpan' || rightType === 'TimeSpan';
        // String equality must use $str_eq (byte-by-byte) — two FIL strings with
        // the same contents are not the same heap pointer. JSON parse always
        // allocates fresh, so json field accesses also need content comparison.
        const isStringEq = (leftType === 'string' && (rightType === 'string' || rightType === 'json')) ||
            (rightType === 'string' && (leftType === 'string' || leftType === 'json'));
        const prefix = isFloat ? 'f64' : isLong ? 'i64' : 'i32';
        // String concatenation needs per-operand emission so we can insert the
        // appropriate numeric/json→string conversion *between* the operand's
        // value-producing instructions and the $str_concat call. The other
        // arithmetic/comparison ops keep the original push-both-then-op shape.
        if (op === '+' && (leftType === 'string' || rightType === 'string')) {
            lines.push(...this.emitExpr(expr.left, ctx));
            if (leftType !== 'string')
                lines.push(this.coerceToString(leftType));
            lines.push(...this.emitExpr(expr.right, ctx));
            if (rightType !== 'string')
                lines.push(this.coerceToString(rightType));
            lines.push('(call $str_concat)');
            return lines;
        }
        lines.push(...this.emitExpr(expr.left, ctx));
        lines.push(...this.emitExpr(expr.right, ctx));
        switch (op) {
            case '+':
                lines.push(`(${prefix}.add)`);
                break;
            case '-':
                lines.push(`(${prefix}.sub)`);
                break;
            case '*':
                lines.push(`(${prefix}.mul)`);
                break;
            case '/':
                lines.push(isFloat ? '(f64.div)' : isLong ? '(i64.div_s)' : '(i32.div_s)');
                break;
            case '%':
                lines.push(isLong ? '(i64.rem_s)' : '(i32.rem_s)');
                break;
            case '**':
                // Simplified: use f64
                lines.push('(f64.const 0) ;; ** not directly supported');
                break;
            case '==':
            case '===':
                if (isStringEq)
                    lines.push('(call $str_eq)');
                else
                    lines.push(isFloat ? '(f64.eq)' : isLong ? '(i64.eq)' : '(i32.eq)');
                break;
            case '!=':
            case '!==':
                if (isStringEq) {
                    lines.push('(call $str_eq)');
                    lines.push('(i32.eqz)');
                }
                else
                    lines.push(isFloat ? '(f64.ne)' : isLong ? '(i64.ne)' : '(i32.ne)');
                break;
            case '<':
                lines.push(isFloat ? '(f64.lt)' : isLong ? '(i64.lt_s)' : '(i32.lt_s)');
                break;
            case '<=':
                lines.push(isFloat ? '(f64.le)' : isLong ? '(i64.le_s)' : '(i32.le_s)');
                break;
            case '>':
                lines.push(isFloat ? '(f64.gt)' : isLong ? '(i64.gt_s)' : '(i32.gt_s)');
                break;
            case '>=':
                lines.push(isFloat ? '(f64.ge)' : isLong ? '(i64.ge_s)' : '(i32.ge_s)');
                break;
            case '&':
                lines.push(isLong ? '(i64.and)' : '(i32.and)');
                break;
            case '|':
                lines.push(isLong ? '(i64.or)' : '(i32.or)');
                break;
            case '^':
                lines.push(isLong ? '(i64.xor)' : '(i32.xor)');
                break;
            case '<<':
                lines.push(isLong ? '(i64.shl)' : '(i32.shl)');
                break;
            case '>>':
                lines.push(isLong ? '(i64.shr_s)' : '(i32.shr_s)');
                break;
            case '>>>':
                lines.push(isLong ? '(i64.shr_u)' : '(i32.shr_u)');
                break;
            case 'instanceof':
                lines.push('(i32.const 0) ;; instanceof');
                break;
            case 'in':
                lines.push('(i32.const 0) ;; in');
                break;
            default:
                lines.push(`(i32.const 0) ;; unsupported op: ${op}`);
        }
        return lines;
    }
    emitLogicalExpression(expr, ctx) {
        const lines = [];
        const leftType = expr.left.filType ?? 'i32';
        switch (expr.operator) {
            case '&&': {
                // a && b: if a then b else a (0)
                lines.push(...this.emitExpr(expr.left, ctx));
                lines.push(`(if (result ${this.watType(leftType)})`);
                lines.push('  (then');
                lines.push(...this.emitExpr(expr.right, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push('  (else');
                lines.push(`    (${this.watType(leftType)}.const 0)`);
                lines.push('  )');
                lines.push(')');
                break;
            }
            case '||': {
                // a || b: if a then a else b
                lines.push(...this.emitExpr(expr.left, ctx));
                lines.push(`(if (result ${this.watType(leftType)})`);
                lines.push('  (then');
                lines.push(...this.emitExpr(expr.left, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push('  (else');
                lines.push(...this.emitExpr(expr.right, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push(')');
                break;
            }
            case '??': {
                // a ?? b: if a != 0 then a else b
                lines.push(...this.emitExpr(expr.left, ctx));
                lines.push(`(if (result ${this.watType(leftType)})`);
                lines.push('  (then');
                lines.push(...this.emitExpr(expr.left, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push('  (else');
                lines.push(...this.emitExpr(expr.right, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push(')');
                break;
            }
        }
        return lines;
    }
    emitUnaryExpression(expr, ctx) {
        const lines = [];
        const argType = expr.argument.filType ?? 'i32';
        switch (expr.operator) {
            case '!':
                lines.push(...this.emitExpr(expr.argument, ctx));
                lines.push('(i32.eqz)');
                break;
            case '-':
                if (argType === 'f64') {
                    lines.push(...this.emitExpr(expr.argument, ctx));
                    lines.push('(f64.neg)');
                }
                else {
                    lines.push('(i32.const 0)');
                    lines.push(...this.emitExpr(expr.argument, ctx));
                    lines.push('(i32.sub)');
                }
                break;
            case '+':
                lines.push(...this.emitExpr(expr.argument, ctx));
                break;
            case '~':
                lines.push(...this.emitExpr(expr.argument, ctx));
                lines.push('(i32.const -1)');
                lines.push('(i32.xor)');
                break;
            case 'typeof':
                // Return "number" or "string" - simplified
                lines.push(`(i32.const ${this.internString('number')})`);
                break;
            case 'void':
                lines.push(...this.emitExpr(expr.argument, ctx));
                lines.push('(drop)');
                lines.push('(i32.const 0)');
                break;
            case 'delete':
                lines.push(...this.emitExpr(expr.argument, ctx));
                lines.push('(drop)');
                lines.push('(i32.const 1)');
                break;
            default:
                lines.push(...this.emitExpr(expr.argument, ctx));
        }
        return lines;
    }
    emitUpdateExpression(expr, ctx) {
        const lines = [];
        const argType = expr.argument.filType ?? 'i32';
        // Get current value
        lines.push(...this.emitExpr(expr.argument, ctx));
        if (expr.prefix) {
            // ++x: increment then return new value
            if (argType === 'f64') {
                lines.push('(f64.const 1)');
                lines.push(expr.operator === '++' ? '(f64.add)' : '(f64.sub)');
            }
            else {
                lines.push('(i32.const 1)');
                lines.push(expr.operator === '++' ? '(i32.add)' : '(i32.sub)');
            }
            // Store back
            lines.push(...this.emitAssignTarget(expr.argument, ctx));
        }
        else {
            // x++: return old value, then increment
            // We need to dup the value... use a temp
            // WAT doesn't have dup; we'd need a local
            // Simplified: just increment (don't track pre-value)
            if (argType === 'f64') {
                lines.push('(f64.const 1)');
                lines.push(expr.operator === '++' ? '(f64.add)' : '(f64.sub)');
            }
            else {
                lines.push('(i32.const 1)');
                lines.push(expr.operator === '++' ? '(i32.add)' : '(i32.sub)');
            }
            lines.push(...this.emitAssignTarget(expr.argument, ctx));
        }
        return lines;
    }
    emitAssignTarget(expr, ctx) {
        // Emit store to target (expects value on stack)
        if (expr.kind === 'Identifier') {
            const name = expr.name;
            return [this.getLocalSet(name, ctx)];
        }
        return ['(drop)'];
    }
    emitConditionalExpression(expr, ctx) {
        const lines = [];
        const resultType = expr.filType ?? 'i32';
        lines.push(...this.emitExpr(expr.test, ctx));
        lines.push(`(if (result ${this.watType(resultType)})`);
        lines.push('  (then');
        lines.push(...this.emitExpr(expr.consequent, ctx).map(l => '    ' + l));
        lines.push('  )');
        lines.push('  (else');
        lines.push(...this.emitExpr(expr.alternate, ctx).map(l => '    ' + l));
        lines.push('  )');
        lines.push(')');
        return lines;
    }
    emitAssignment(expr, ctx) {
        const lines = [];
        const op = expr.operator;
        if (op === '=') {
            lines.push(...this.emitExpr(expr.right, ctx));
            lines.push(...this.emitAssignTarget(expr.left, ctx));
            // Return the value (re-load)
            lines.push(...this.emitExpr(expr.left, ctx));
        }
        else {
            // Compound assignment: x += y => x = x + y
            const baseOp = op.slice(0, -1); // remove '='
            lines.push(...this.emitExpr(expr.left, ctx));
            lines.push(...this.emitExpr(expr.right, ctx));
            // Apply op
            const leftType = expr.left.filType ?? 'i32';
            const isFloat = leftType === 'f64';
            switch (baseOp) {
                case '+':
                    if (leftType === 'string')
                        lines.push('(call $str_concat)');
                    else
                        lines.push(isFloat ? '(f64.add)' : '(i32.add)');
                    break;
                case '-':
                    lines.push(isFloat ? '(f64.sub)' : '(i32.sub)');
                    break;
                case '*':
                    lines.push(isFloat ? '(f64.mul)' : '(i32.mul)');
                    break;
                case '/':
                    lines.push(isFloat ? '(f64.div)' : '(i32.div_s)');
                    break;
                case '%':
                    lines.push('(i32.rem_s)');
                    break;
                default: lines.push('(i32.add)'); // fallback
            }
            lines.push(...this.emitAssignTarget(expr.left, ctx));
            lines.push(...this.emitExpr(expr.left, ctx));
        }
        return lines;
    }
    emitCallExpression(expr, ctx) {
        const lines = [];
        // Handle await - this is the core of async compilation
        // (handled separately in emitAwaitExpression)
        // Direct function calls
        if (expr.callee.kind === 'Identifier') {
            const name = expr.callee.name;
            // Built-in functions — both go through the deterministic protocol
            // helpers so replays read the recorded value from stdin.
            if (name === 'getDateTime') {
                return ['(call $protocol_get_datetime)'];
            }
            if (name === 'getUuid') {
                return ['(call $protocol_get_uuid)'];
            }
            // User-defined function call
            const fnInfo = this.functions.get(name);
            if (fnInfo) {
                for (const arg of expr.arguments) {
                    lines.push(...this.emitExpr(arg, ctx));
                }
                lines.push(`(call $${name})`);
                return lines;
            }
            // Unknown - just return 0
            return [`(i32.const 0) ;; unknown call: ${name}`];
        }
        // Member expression calls: obj.method(args) or Namespace.method(args)
        if (expr.callee.kind === 'MemberExpression') {
            return this.emitMethodCall(expr.callee, expr.arguments, ctx);
        }
        // Other
        for (const arg of expr.arguments) {
            lines.push(...this.emitExpr(arg, ctx));
        }
        lines.push('(drop)');
        lines.push('(i32.const 0)');
        return lines;
    }
    emitMethodCall(member, args, ctx) {
        const lines = [];
        if (member.property.kind !== 'Identifier') {
            // Computed - skip
            return ['(i32.const 0) ;; computed method call'];
        }
        const method = member.property.name;
        // Namespace calls
        if (member.object.kind === 'Identifier') {
            const ns = member.object.name;
            if (ns === 'console' && method === 'log') {
                for (const arg of args) {
                    const argType = arg.filType ?? 'json';
                    if (argType === 'i32' || argType === 'bool') {
                        lines.push(...this.emitExpr(arg, ctx));
                        lines.push('(call $i32_to_str)');
                        lines.push('(call $host_console_log)');
                    }
                    else if (argType === 'f64') {
                        lines.push(...this.emitExpr(arg, ctx));
                        lines.push('(call $f64_to_str)');
                        lines.push('(call $host_console_log)');
                    }
                    else {
                        lines.push(...this.emitExpr(arg, ctx));
                        lines.push('(call $host_console_log)');
                    }
                }
                return lines;
            }
            if (ns === 'JSON') {
                if (method === 'stringify') {
                    if (args.length > 0) {
                        const argType = args[0].filType ?? 'json';
                        const argExpr = args[0];
                        if (argType === 'string') {
                            lines.push(...this.emitExpr(argExpr, ctx));
                        }
                        else if (argType === 'i32') {
                            lines.push(...this.emitExpr(argExpr, ctx));
                            lines.push('(call $i32_to_str)');
                        }
                        else {
                            // json type → use $_json_stringify_value with last known tag
                            // This works for parsed values (tag saved by parser) and known literal types
                            lines.push(...this.emitExpr(argExpr, ctx));
                            lines.push('(global.get $_json_tag)');
                            lines.push('(call $_json_stringify_value)');
                        }
                    }
                    else {
                        lines.push('(i32.const 0)');
                    }
                    return lines;
                }
                if (method === 'parse') {
                    if (args.length > 0) {
                        lines.push(...this.emitExpr(args[0], ctx));
                        lines.push('(call $json_parse)');
                    }
                    else {
                        lines.push('(call $json_obj_new (i32.const 0))');
                    }
                    return lines;
                }
                return ['(i32.const 0)'];
            }
            if (ns === 'Math') {
                if (method === 'PI')
                    return ['(f64.const 3.141592653589793)'];
                if (method === 'E')
                    return ['(f64.const 2.718281828459045)'];
                const mathMap = {
                    abs: 'f64.abs', ceil: 'f64.ceil', floor: 'f64.floor',
                    sqrt: 'f64.sqrt', trunc: 'f64.trunc',
                    min: 'f64.min', max: 'f64.max',
                    sin: 'f64.sin' /* not actually in MVP but common */,
                };
                if (mathMap[method]) {
                    for (const arg of args)
                        lines.push(...this.emitExpr(arg, ctx));
                    lines.push(`(${mathMap[method]})`);
                    return lines;
                }
                // Fallback for unsupported math
                for (const arg of args) {
                    lines.push(...this.emitExpr(arg, ctx));
                }
                lines.push('(f64.const 0)');
                return lines;
            }
            if (ns === 'Number') {
                if (method === 'parseInt') {
                    if (args.length > 0) {
                        // Simplified: return 0 (would need actual parsing)
                        lines.push(...this.emitExpr(args[0], ctx));
                        lines.push('(drop)');
                    }
                    lines.push('(i32.const 0)');
                    return lines;
                }
                if (method === 'parseFloat') {
                    lines.push('(f64.const 0)');
                    return lines;
                }
            }
            if (ns === 'Object') {
                // Simplified object methods
                for (const arg of args) {
                    lines.push(...this.emitExpr(arg, ctx));
                    lines.push('(drop)');
                }
                lines.push('(i32.const 0)');
                return lines;
            }
            if (ns === 'DateTime') {
                if (method === 'now') {
                    return ['(call $protocol_get_datetime)'];
                }
                if (method === 'fromEpochMillis') {
                    // Argument is i64 — pass through unchanged.
                    if (args.length > 0)
                        lines.push(...this.emitExpr(args[0], ctx));
                    else
                        lines.push('(i64.const 0)');
                    return lines;
                }
                if (method === 'fromISOString') {
                    // Phase 2 provides the parser; the argument string pointer is consumed by it.
                    if (args.length > 0)
                        lines.push(...this.emitExpr(args[0], ctx));
                    else
                        lines.push(`(i32.const ${this.internString('')})`);
                    lines.push('(call $datetime_parse_iso)');
                    return lines;
                }
                for (const arg of args)
                    lines.push(...this.emitExpr(arg, ctx));
                lines.push('(i64.const 0)');
                return lines;
            }
            if (ns === 'TimeSpan') {
                if (method === 'fromMillis' || method === 'fromSeconds' || method === 'fromMinutes' ||
                    method === 'fromHours' || method === 'fromDays') {
                    if (args.length > 0)
                        lines.push(...this.emitExpr(args[0], ctx));
                    else
                        lines.push('(i64.const 0)');
                    // Multiply by the appropriate factor
                    const factor = method === 'fromMillis' ? 1n :
                        method === 'fromSeconds' ? 1000n :
                            method === 'fromMinutes' ? 60000n :
                                method === 'fromHours' ? 3600000n :
                                    86400000n;
                    if (factor !== 1n) {
                        lines.push(`(i64.const ${factor})`);
                        lines.push('(i64.mul)');
                    }
                    return lines;
                }
                for (const arg of args)
                    lines.push(...this.emitExpr(arg, ctx));
                lines.push('(i64.const 0)');
                return lines;
            }
        }
        // Instance method calls: str.method(...), arr.method(...)
        const objType = member.object.filType ?? 'json';
        // DateTime instance methods (objType === 'DateTime', i64 epoch ms)
        if (objType === 'DateTime') {
            if (method === 'add') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.add)');
                return lines;
            }
            if (method === 'subtract') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.sub)');
                return lines;
            }
            if (method === 'diff') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.sub)');
                return lines;
            }
            if (method === 'toEpochMillis') {
                lines.push(...this.emitExpr(member.object, ctx));
                return lines;
            }
            if (method === 'equals') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.eq)');
                return lines;
            }
            if (method === 'isBefore') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.lt_s)');
                return lines;
            }
            if (method === 'isAfter') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.gt_s)');
                return lines;
            }
            // Phase 2: calendar getters + toISOString stubbed; real implementation fills these in.
            if (method === 'toISOString') {
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push('(call $datetime_to_iso)');
                return lines;
            }
            if (method === 'getYear' || method === 'getMonth' || method === 'getDay' ||
                method === 'getDayOfWeek' || method === 'getHour' || method === 'getMinute' ||
                method === 'getSecond' || method === 'getMillisecond') {
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push(`(call $datetime_${method.slice(3).toLowerCase()})`);
                return lines;
            }
        }
        // TimeSpan instance methods (objType === 'TimeSpan', i64 signed ms)
        if (objType === 'TimeSpan') {
            if (method === 'add') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.add)');
                return lines;
            }
            if (method === 'subtract') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.sub)');
                return lines;
            }
            if (method === 'multiply') {
                // factor is f64 — convert i64 → f64, multiply, truncate
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push('(f64.convert_i64_s)');
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(f64.const 1)');
                lines.push('(f64.mul)');
                lines.push('(i64.trunc_f64_s)');
                return lines;
            }
            if (method === 'negate') {
                lines.push('(i64.const 0)');
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push('(i64.sub)');
                return lines;
            }
            if (method === 'totalMillis') {
                lines.push(...this.emitExpr(member.object, ctx));
                return lines;
            }
            if (method === 'totalSeconds' || method === 'totalMinutes' || method === 'totalHours' ||
                method === 'totalDays') {
                const denom = method === 'totalSeconds' ? 1000 :
                    method === 'totalMinutes' ? 60000 :
                        method === 'totalHours' ? 3600000 :
                            86400000;
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push('(f64.convert_i64_s)');
                lines.push(`(f64.const ${denom})`);
                lines.push('(f64.div)');
                return lines;
            }
            if (method === 'equals') {
                lines.push(...this.emitExpr(member.object, ctx));
                if (args.length > 0)
                    lines.push(...this.emitExpr(args[0], ctx));
                else
                    lines.push('(i64.const 0)');
                lines.push('(i64.eq)');
                return lines;
            }
        }
        if (objType === 'string') {
            if (method === 'length') {
                lines.push(...this.emitExpr(member.object, ctx));
                lines.push('(call $str_length)');
                return lines;
            }
            if (method === 'concat') {
                lines.push(...this.emitExpr(member.object, ctx));
                for (const arg of args) {
                    lines.push(...this.emitExpr(arg, ctx));
                    lines.push('(call $str_concat)');
                }
                return lines;
            }
            if (method === 'toString' || method === 'trim' || method === 'toUpperCase' || method === 'toLowerCase') {
                // Simplified: return same string
                lines.push(...this.emitExpr(member.object, ctx));
                return lines;
            }
        }
        // Default: emit object, emit args, return 0
        lines.push(...this.emitExpr(member.object, ctx));
        lines.push('(drop)');
        for (const arg of args) {
            lines.push(...this.emitExpr(arg, ctx));
            lines.push('(drop)');
        }
        lines.push('(i32.const 0)');
        return lines;
    }
    emitMemberExpression(expr, ctx) {
        const objType = expr.object.filType ?? 'json';
        if (!expr.computed && expr.property.kind === 'Identifier') {
            const prop = expr.property.name;
            if (prop === 'length') {
                return [
                    ...this.emitExpr(expr.object, ctx),
                    '(call $str_length)',
                ];
            }
            // Property access on json → $json_get_field with interned key
            if (objType === 'json') {
                const keyAddr = this.internString(prop);
                return [
                    ...this.emitExpr(expr.object, ctx),
                    `(i32.const ${keyAddr})`,
                    '(call $json_get_field)',
                ];
            }
            // Non-json property access: return 0
            return [
                ...this.emitExpr(expr.object, ctx),
                '(drop)',
                `(i32.const 0) ;; .${prop}`,
            ];
        }
        // Computed access on json
        if (objType === 'json') {
            // Numeric index → array access; string index → field access
            const propType = expr.property.filType ??
                (expr.property.kind === 'Literal' && typeof expr.property.value === 'number' ? 'i32' : 'string');
            if (propType === 'i32' || propType === 'f64') {
                return [
                    ...this.emitExpr(expr.object, ctx),
                    ...this.emitExpr(expr.property, ctx),
                    '(call $json_array_get)',
                ];
            }
            return [
                ...this.emitExpr(expr.object, ctx),
                ...this.emitExpr(expr.property, ctx),
                '(call $json_get_field)',
            ];
        }
        // Other computed access
        return [
            ...this.emitExpr(expr.object, ctx),
            '(drop)',
            ...this.emitExpr(expr.property, ctx),
            '(drop)',
            '(i32.const 0)',
        ];
    }
    emitAwaitExpression(expr, ctx) {
        if (!ctx.isAsync) {
            // Not in async context - just emit the argument (shouldn't happen in well-typed FIL)
            return this.emitExpr(expr.argument, ctx);
        }
        const lines = [];
        const arg = expr.argument;
        if (arg.kind === 'CallExpression') {
            const callee = arg.callee;
            if (callee.kind === 'Identifier' && callee.name === 'executeNode') {
                // await executeNode(nodeName, input)
                // WASI model: call $protocol_execute_node which either returns result or calls proc_exit
                const nodeArg = arg.arguments[0];
                const inputArg = arg.arguments[1];
                lines.push('(call $protocol_execute_node');
                if (nodeArg)
                    lines.push(...this.emitActionNameArg(nodeArg, ctx).map(l => '  ' + l));
                else
                    lines.push(`  (i32.const ${this.internString('')})`);
                if (inputArg)
                    lines.push(...this.emitExpr(inputArg, ctx).map(l => '  ' + l));
                else
                    lines.push(`  (i32.const ${this.internString('')})`);
                lines.push(')');
                return lines;
            }
            if (callee.kind === 'Identifier' && callee.name === 'executeTimer') {
                // await executeTimer(when) — overload by argument type.
                const argExpr = arg.arguments[0];
                const argType = argExpr?.filType ?? 'TimeSpan';
                const helper = argType === 'DateTime'
                    ? '$protocol_execute_timer_dt'
                    : '$protocol_execute_timer_ts';
                lines.push(`(call ${helper}`);
                if (argExpr)
                    lines.push(...this.emitExpr(argExpr, ctx).map(l => '  ' + l));
                else
                    lines.push('  (i64.const 0)');
                lines.push(')');
                return lines;
            }
            // Promise.all([...]) and Promise.any([...]) (Phases 4 and 5)
            if (callee.kind === 'MemberExpression' &&
                callee.object.kind === 'Identifier' && callee.object.name === 'Promise' &&
                callee.property.kind === 'Identifier' &&
                (callee.property.name === 'all' || callee.property.name === 'any')) {
                const arrArg = arg.arguments[0];
                if (!arrArg || arrArg.kind !== 'ArrayExpression') {
                    throw new Error(`Promise.${callee.property.name} requires an array literal argument`);
                }
                if (callee.property.name === 'all') {
                    return this.emitPromiseAll(arrArg.elements, ctx);
                }
                return this.emitPromiseAny(arrArg.elements, ctx);
            }
            if (callee.kind === 'Identifier') {
                const fnInfo = this.functions.get(callee.name);
                if (fnInfo && fnInfo.isAsync) {
                    // await userAsyncFn(args) — WASI model: direct call (function handles protocol internally)
                    return this.emitCallExpression(arg, ctx);
                }
            }
        }
        // Generic await — just evaluate the expression
        return this.emitExpr(arg, ctx);
    }
    // ─── Promise combinator emission ────────────────────────────────────────
    //
    // Allowed array entries are limited to direct calls of `executeNode` /
    // `executeTimer`. Wrapping these in `await` is also accepted for ergonomics —
    // the await is stripped, since the combinator owns suspension semantics.
    // Anything else is rejected at compile time.
    /**
     * Emit the WAT that loads the node-name string for `executeNode`'s first
     * argument. If the arg is an Identifier that resolves to a declared action,
     * substitute a string-literal load of the action's effective protocol id
     * (the action ref → string lowering). Otherwise fall through to normal
     * expression emission.
     */
    emitActionNameArg(expr, ctx) {
        if (expr.kind === 'Identifier') {
            const id = this.actionIds.get(expr.name);
            if (id !== undefined) {
                const addr = this.internString(id);
                return [`(i32.const ${addr})`];
            }
        }
        return this.emitExpr(expr, ctx);
    }
    decisionShape(elem) {
        let inner = elem;
        if (inner.kind === 'AwaitExpression')
            inner = inner.argument;
        if (inner.kind !== 'CallExpression' || inner.callee.kind !== 'Identifier') {
            throw new Error('Promise.all/any entries must be executeNode(...) or executeTimer(...) calls');
        }
        const call = inner;
        const fname = call.callee.name;
        if (fname === 'executeNode') {
            return {
                kind: 'node',
                nameArg: call.arguments[0],
                inputArg: call.arguments[1],
            };
        }
        if (fname === 'executeTimer') {
            const argType = call.arguments[0]?.filType;
            const arg = call.arguments[0];
            if (argType === 'DateTime')
                return { kind: 'timer-dt', arg };
            return { kind: 'timer-ts', arg };
        }
        throw new Error(`Promise.all/any entries must be executeNode or executeTimer; got ${fname}`);
    }
    emitDecisionWrite(elem, ctx) {
        const shape = this.decisionShape(elem);
        const lines = [];
        if (shape.kind === 'node') {
            lines.push('(call $write_node_decision');
            lines.push(...this.emitActionNameArg(shape.nameArg, ctx).map(l => '  ' + l));
            lines.push(...this.emitExpr(shape.inputArg, ctx).map(l => '  ' + l));
            lines.push(')');
        }
        else if (shape.kind === 'timer-dt') {
            lines.push('(call $write_timer_dt_decision');
            lines.push(...this.emitExpr(shape.arg, ctx).map(l => '  ' + l));
            lines.push(')');
        }
        else {
            lines.push('(call $write_timer_ts_decision');
            lines.push(...this.emitExpr(shape.arg, ctx).map(l => '  ' + l));
            lines.push(')');
        }
        return lines;
    }
    emitPromiseAll(elements, ctx) {
        const lines = [];
        const N = elements.length;
        // Always emit the marker and decisions to stdout. The host needs them to
        // know what work is in flight; on a successful replay it just sees the
        // same decisions it already executed.
        lines.push(`(call $write_all_marker (i32.const ${N}))`);
        for (const elem of elements) {
            lines.push(...this.emitDecisionWrite(elem, ctx));
        }
        // Stdin must have "all: N\n" followed by N records — otherwise exit.
        lines.push(`(if (i32.eqz (call $match_all_marker (i32.const ${N})))`);
        lines.push('  (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        // Allocate result array and store reads in order.
        const arrLocal = `__all_arr_${ctx.tmpCount++}`;
        lines.push(`(call $json_array_new (i32.const ${N}))`);
        lines.push(`(local.set $${arrLocal})`);
        for (let i = 0; i < elements.length; i++) {
            const shape = this.decisionShape(elements[i]);
            lines.push('(call $json_array_set');
            lines.push(`  (local.get $${arrLocal})`);
            lines.push(`  (i32.const ${i})`);
            if (shape.kind === 'node') {
                // Match the single-await semantics: keep the raw output JSON string.
                // Callers JSON.parse it explicitly when they need field access.
                lines.push('  (call $read_node_result');
                lines.push(...this.emitActionNameArg(shape.nameArg, ctx).map(l => '    ' + l));
                lines.push('  )');
                lines.push('  (i32.const 0)'); // STRING tag
            }
            else {
                // Timer result is i64 (epoch ms). Encode as ISO string for the array.
                lines.push('  (call $datetime_to_iso (call $read_timer_result))');
                lines.push('  (i32.const 0)'); // STRING tag
            }
            lines.push(')');
        }
        // Push the array on stack and tag the global so JSON.stringify works.
        lines.push('(global.set $_json_tag (i32.const 6))');
        lines.push(`(local.get $${arrLocal})`);
        return lines;
    }
    emitPromiseAny(elements, ctx) {
        const lines = [];
        const N = elements.length;
        // Always emit the marker and decisions to stdout.
        lines.push(`(call $write_race_marker (i32.const ${N}))`);
        for (const elem of elements) {
            lines.push(...this.emitDecisionWrite(elem, ctx));
        }
        // Match stdin for the race marker. K = winner index (>=0), -1 = no marker
        // (first replay), -2 = marker present but race in flight.
        const kLocal = `__any_k_${ctx.tmpCount++}`;
        lines.push(`(local.set $${kLocal} (call $match_race_marker (i32.const ${N})))`);
        lines.push(`(if (i32.lt_s (local.get $${kLocal}) (i32.const 0))`);
        lines.push('  (then (call $out_flush) (call $wasi_proc_exit (i32.const 0)) (unreachable)))');
        // Read N stdin records to advance the cursor past them. The K-th one is
        // the winner; others are cancellation placeholders. We don't keep the
        // winner's value — callers get only the index and structure their code
        // around it.
        for (let i = 0; i < N; i++) {
            const shape = this.decisionShape(elements[i]);
            lines.push(`(if (i32.eq (local.get $${kLocal}) (i32.const ${i}))`);
            lines.push('  (then');
            if (shape.kind === 'node') {
                lines.push('    (drop (call $read_node_result');
                lines.push(...this.emitActionNameArg(shape.nameArg, ctx).map(l => '      ' + l));
                lines.push('    ))');
            }
            else {
                lines.push('    (drop (call $read_timer_result))');
            }
            lines.push('  )');
            lines.push('  (else');
            if (shape.kind === 'node') {
                lines.push('    (call $skip_cancelled_node)');
            }
            else {
                lines.push('    (call $skip_cancelled_timer)');
            }
            lines.push('  )');
            lines.push(')');
        }
        lines.push(`(local.get $${kLocal})`);
        return lines;
    }
    emitNewExpression(expr, ctx) {
        // new Error(msg) - simplified
        if (expr.callee.kind === 'Identifier' && expr.callee.name === 'Error') {
            if (expr.arguments.length > 0) {
                return this.emitExpr(expr.arguments[0], ctx);
            }
            return [`(i32.const ${this.internString('Error')})`];
        }
        return ['(i32.const 0)'];
    }
    // ─── Local variable helpers ────────────────────────────────────────────────
    getLocalName(name, ctx) {
        if (ctx.isAsync && ctx.smInfo) {
            const slot = ctx.smInfo.frameLayout.slots.find(s => s.name === name);
            if (slot)
                return `$tmp_${name}`;
        }
        return `$${name}`;
    }
    getLocalRef(name, ctx) {
        if (ctx.isAsync && ctx.smInfo) {
            const slot = ctx.smInfo.frameLayout.slots.find(s => s.name === name);
            if (slot) {
                // Load from frame
                return `(i32.load offset=${slot.offset} (local.get $fp))`;
            }
        }
        return `(local.get $${name})`;
    }
    getLocalSet(name, ctx) {
        if (ctx.isAsync && ctx.smInfo) {
            const slot = ctx.smInfo.frameLayout.slots.find(s => s.name === name);
            if (slot) {
                return `(i32.store offset=${slot.offset} (local.get $fp))`;
            }
        }
        return `(local.set $${name})`;
    }
    // ─── WAT type mapping ─────────────────────────────────────────────────────
    watType(t) {
        if (typeof t === 'string') {
            switch (t) {
                case 'i32': return 'i32';
                case 'i64': return 'i64';
                case 'f64': return 'f64';
                case 'bool': return 'i32';
                case 'string': return 'i32'; // pointer
                case 'json': return 'i32'; // pointer
                case 'DateTime': return 'i64'; // epoch ms
                case 'TimeSpan': return 'i64'; // signed ms
                case 'void': return 'void';
                case 'unknown': return 'i32';
                default: return 'i32';
            }
        }
        if (t.kind === 'Promise') {
            if (t.inner === 'void')
                return 'void';
            if (t.inner === 'DateTime' || t.inner === 'TimeSpan' || t.inner === 'i64')
                return 'i64';
            return 'i32';
        }
        if (t.kind === 'array')
            return 'i32'; // pointer
        return 'i32';
    }
    defaultValueForType(t) {
        const wat = this.watType(t);
        switch (wat) {
            case 'i64': return '(i64.const 0)';
            case 'f64': return '(f64.const 0)';
            case 'void': return '';
            case '':
                return '';
            case 'i32':
            default:
                return '(i32.const 0)';
        }
    }
    // ─── Collect local variables ──────────────────────────────────────────────
    collectLocals(block, ctx) {
        const locals = new Map();
        // These counters track ctx.tmpCount at emission time.
        // emitObjectExpression and emitArrayExpression both increment ctx.tmpCount,
        // so we use a unified counter that matches the traversal order.
        let tmpCount = 0;
        const visit = (node) => {
            if (node.kind === 'VariableDeclaration') {
                const v = node;
                const type = v.filType ?? v.typeAnnotation ?? 'json';
                if (!ctx.localVars.has(v.name) && !locals.has(v.name)) {
                    locals.set(v.name, type);
                }
            }
            // Loop constructs consume a tmpCount slot during emission (for label
            // naming). Increment here to keep this counter aligned with emit-side
            // ctx.tmpCount, otherwise nested ObjectExpression/ArrayExpression temp
            // locals get declared with one suffix and used with another.
            if (node.kind === 'WhileStatement' || node.kind === 'ForStatement') {
                tmpCount++;
            }
            if (node.kind === 'ForOfStatement') {
                const f = node;
                const slot = tmpCount++;
                locals.set(`__forof_iter_${slot}`, 'i32');
                locals.set(`__forof_idx_${slot}`, 'i32');
                locals.set(`__forof_len_${slot}`, 'i32');
                if (!ctx.localVars.has(f.item) && !locals.has(f.item)) {
                    // Element type isn't carried on the AST; default to json (i32 ptr).
                    // Typed-array iteration is rare in practice and would still load as
                    // i32 from json_array_get.
                    locals.set(f.item, 'json');
                }
            }
            // Pre-declare temp locals for ObjectExpression/ArrayExpression emission
            if (node.kind === 'ObjectExpression') {
                const o = node;
                if (o.properties.length > 0) {
                    locals.set(`__obj_tmp_${tmpCount++}`, 'i32');
                }
            }
            else if (node.kind === 'ArrayExpression') {
                const a = node;
                if (a.elements.length > 0) {
                    locals.set(`__arr_tmp_${tmpCount++}`, 'i32');
                }
            }
            // Promise.all([...]) and Promise.any([...]) both use temp locals.
            // emitPromiseAll uses 1 counter slot (__all_arr_N).
            // emitPromiseAny uses 1 counter slot (__any_k_N).
            // The wrapping ArrayExpression is consumed directly, so it does NOT
            // contribute its own __arr_tmp slot.
            if (node.kind === 'CallExpression') {
                const c = node;
                if (c.callee.kind === 'MemberExpression' &&
                    c.callee.object.kind === 'Identifier' &&
                    c.callee.object.name === 'Promise' &&
                    c.callee.property.kind === 'Identifier') {
                    const propName = c.callee.property.name;
                    if (propName === 'all' || propName === 'any') {
                        // Walk entries FIRST so any nested temps (object literals inside
                        // JSON.stringify arguments to executeNode, etc.) claim their
                        // counter slots in the same order emit will consume them.
                        // emitPromiseAll/Any allocate their own all_arr/any_k slot AFTER
                        // walking entries, so this pre-scan order matches emit order;
                        // declaring all_arr/any_k first here would desync the counter.
                        if (c.arguments[0]?.kind === 'ArrayExpression') {
                            for (const el of c.arguments[0].elements) {
                                visit(el);
                            }
                        }
                        if (propName === 'all') {
                            locals.set(`__all_arr_${tmpCount++}`, 'i32');
                        }
                        else {
                            locals.set(`__any_k_${tmpCount++}`, 'i32');
                        }
                        return;
                    }
                }
            }
            // walkNodeShallow intentionally does not recurse into AwaitExpression
            // (await counting needs that). For local collection we DO want to see
            // any literals/temps inside the awaited expression — emission walks them.
            if (node.kind === 'AwaitExpression') {
                visit(node.argument);
                return;
            }
            this.walkNodeShallow(node, visit);
        };
        for (const stmt of block.body) {
            visit(stmt);
        }
        return locals;
    }
    // ─── Pre-scan for string literals ────────────────────────────────────────
    preScanStrings(program) {
        // Initialize empty string
        this.internString('');
        const visitExpr = (expr) => {
            if (expr.kind === 'Literal' && typeof expr.value === 'string') {
                this.internString(expr.value);
                return;
            }
            if (expr.kind === 'TemplateLiteral') {
                for (const q of expr.quasis)
                    this.internString(q);
                for (const e of expr.expressions)
                    visitExpr(e);
                return;
            }
            // AwaitExpression is skipped by walkNodeShallow, so handle it explicitly
            if (expr.kind === 'AwaitExpression') {
                visitExpr(expr.argument);
                return;
            }
            this.walkNodeShallow(expr, (n) => {
                if (AST.isExpression(n)) {
                    try {
                        visitExpr(n);
                    }
                    catch { }
                }
            });
        };
        const visitStmt = (stmt) => {
            this.walkNodeShallow(stmt, (n) => {
                if (AST.isExpression(n)) {
                    try {
                        visitExpr(n);
                    }
                    catch { }
                }
                else if (AST.isStatement(n)) {
                    try {
                        visitStmt(n);
                    }
                    catch { }
                }
            });
        };
        for (const fn of program.functions) {
            for (const stmt of fn.body.body) {
                visitStmt(stmt);
            }
        }
        // Intern every action's effective protocol id — executeNode(actionRef, …)
        // call sites lower the identifier to a string-literal load of that id.
        for (const a of program.actions) {
            this.internString(resolveActionId(a));
        }
    }
}
exports.WatEmitter = WatEmitter;
// ─── Main emit entry point ──────────────────────────────────────────────────
// ─── Fixed WASI memory layout constants ────────────────────────────────────
// These are raw byte addresses in WAT linear memory.
WatEmitter.SCRATCH_ADDR = 0x0008; // IOV: 8 bytes (buf_ptr + buf_len)
WatEmitter.NREAD_ADDR = 0x0010; // nread/nwritten: 4 bytes
WatEmitter.STDIN_BUF = 0x2000; // 16 KB stdin buffer
WatEmitter.STDIN_SIZE = 0x4000;
WatEmitter.OUT_BUF = 0x6000; // 8 KB output buffer
WatEmitter.HEAP_START = 0x8000; // bump allocator starts here
// Protocol string offsets (raw bytes, no FIL length prefix)
// --- stdout literals (full decision format) ---
WatEmitter.P_EXEC_NODE = 0x0020; // "executeNode:\n  name: "          21 bytes
WatEmitter.P_EXEC_LEN = 21;
WatEmitter.P_INPUT = 0x0035; // "\n  input: |\n    "              16 bytes
WatEmitter.P_INPUT_LEN = 16;
WatEmitter.P_END = 0x0045; // "\n\n"                              2 bytes
WatEmitter.P_END_LEN = 2;
// --- stdin literals (compact history format) ---
WatEmitter.P_NODE = 0x0047; // "node: "                           6 bytes
WatEmitter.P_NODE_LEN = 6;
WatEmitter.P_OUTPUT = 0x004d; // "\n  output: "                    11 bytes
WatEmitter.P_OUTPUT_LEN = 11;
// --- shared ---
WatEmitter.P_FLOW_DONE = 0x0058; // "flowCompleted:\n  success: true\n"   31 bytes
WatEmitter.P_FLOW_LEN = 31;
// --- Phase 3: executeTimer ---
WatEmitter.P_TIMER_DL = 0x0078; // "executeTimer:\n  deadline: "    26 bytes
WatEmitter.P_TIMER_DL_LEN = 26;
WatEmitter.P_TIMER_DUR = 0x0092; // "executeTimer:\n  duration: "    26 bytes
WatEmitter.P_TIMER_DUR_LEN = 26;
WatEmitter.P_TIMER = 0x00ac; // "timer: "                          7 bytes
WatEmitter.P_TIMER_LEN = 7;
WatEmitter.P_NL = 0x00b3; // "\n"                               1 byte
WatEmitter.P_NL_LEN = 1;
// --- Phase 4-5: parallel/race markers ---
WatEmitter.P_ALL = 0x00b4; // "all: "                            5 bytes
WatEmitter.P_ALL_LEN = 5;
WatEmitter.P_RACE = 0x00b9; // "race: "                           6 bytes
WatEmitter.P_RACE_LEN = 6;
WatEmitter.P_WINNER = 0x00bf; // " winner="                         8 bytes
WatEmitter.P_WINNER_LEN = 8;
WatEmitter.P_NODE_CANC = 0x00c7; // "nodeCancelled:\n"                15 bytes
WatEmitter.P_NODE_CANC_LEN = 15;
WatEmitter.P_TIMER_CANC = 0x00d6; // "timerCancelled:\n"               16 bytes
WatEmitter.P_TIMER_CANC_LEN = 16;
// --- Phase 6: deterministic host calls (now / uuid) ---
WatEmitter.P_NOW = 0x00e6; // "now: "                            5 bytes
WatEmitter.P_NOW_LEN = 5;
WatEmitter.P_UUID = 0x00eb; // "uuid: "                           6 bytes
WatEmitter.P_UUID_LEN = 6;
/**
 * Resolve an action's protocol id: the `id` field in its body (a string
 * literal) when present, otherwise the action's declared identifier.
 */
function resolveActionId(action) {
    if (!action.fields)
        return action.name;
    const idProp = action.fields.properties.find((p) => p.key === 'id');
    if (idProp && idProp.value.kind === 'Literal' && typeof idProp.value.value === 'string') {
        return idProp.value.value;
    }
    return action.name;
}
//# sourceMappingURL=wat_emitter.js.map