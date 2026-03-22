// Complex coverage test — edge cases for V8 range nesting.
// Total: 7 testable features, each triggered by a button.
// Dead code at bottom should NEVER be covered.
var el = document.getElementById("log");
function log(msg) { el.textContent += msg + "\n"; }
log("Complex test loaded.");

// 1. Nested functions (outer calls inner)
function outerA() {
    log("outerA start");
    function innerA1() {
        log("innerA1");
        return 10;
    }
    function innerA2() {
        log("innerA2");
        return 20;
    }
    var r = innerA1() + innerA2();
    log("outerA done: " + r);
    return r;
}

// 2. Conditional branches — only one side runs
function branchB(flag) {
    log("branchB called with " + flag);
    if (flag) {
        var x = 1;
        var y = 2;
        var z = x + y;
        log("branchB TRUE path: " + z);
        return z;
    } else {
        var a = 100;
        var b = 200;
        var c = a + b;
        log("branchB FALSE path: " + c);
        return c;
    }
}

// 3. Callback / higher-order function
function withCallback(arr, cb) {
    log("withCallback processing " + arr.length + " items");
    var results = [];
    for (var i = 0; i < arr.length; i++) {
        results.push(cb(arr[i]));
    }
    log("withCallback done: " + results.join(","));
    return results;
}

// 4. Closure (factory pattern)
function makeCounter(start) {
    var count = start;
    log("makeCounter from " + start);
    function increment() {
        count++;
        return count;
    }
    function decrement() {
        count--;
        return count;
    }
    function getCount() {
        return count;
    }
    return { increment: increment, decrement: decrement, getCount: getCount };
}

// 5. IIFE (executes on load)
var iifeResult = (function() {
    var secret = 42;
    log("IIFE ran with secret=" + secret);
    return secret * 2;
})();
log("IIFE result: " + iifeResult);

// 6. Try/catch — error path
function riskyE(shouldThrow) {
    log("riskyE called, throw=" + shouldThrow);
    try {
        if (shouldThrow) {
            throw new Error("intentional");
        }
        log("riskyE success path");
        return "ok";
    } catch (e) {
        log("riskyE caught: " + e.message);
        return "caught";
    } finally {
        log("riskyE finally");
    }
}

// 7. Switch statement with multiple cases
function switchF(val) {
    log("switchF called with " + val);
    var result;
    switch (val) {
        case "a":
            result = "alpha";
            break;
        case "b":
            result = "beta";
            break;
        case "c":
            result = "gamma";
            break;
        default:
            result = "unknown";
            break;
    }
    log("switchF result: " + result);
    return result;
}

// ----- DEAD CODE (should never execute) -----
function deadCode1() {
    log("DEAD CODE 1 — this should never run");
    var x = 999;
    var y = x * x;
    return y;
}

function deadCode2() {
    log("DEAD CODE 2 — this should never run");
    for (var i = 0; i < 100; i++) {
        Math.sqrt(i);
    }
    return "dead";
}
