// Coverage test file — 4 functions, ~equal size.
// On load: only the log() helper runs.
// Each button click should increase coverage by ~20%.
var el = document.getElementById("log");
function log(msg) { el.textContent += msg + "\n"; }
log("Page loaded. Click buttons A-D to increase coverage.");

// ---- Function A (click button A) ----
function funcA() {
    var x = 1;
    var y = 2;
    var z = x + y;
    log("A executed: " + z);
    return z;
}

// ---- Function B (click button B) ----
function funcB() {
    var items = ["one", "two", "three"];
    var result = items.join(", ");
    log("B executed: " + result);
    return result;
}

// ---- Function C (click button C) ----
function funcC() {
    var sum = 0;
    for (var i = 0; i < 10; i++) {
        sum += i;
    }
    log("C executed: " + sum);
    return sum;
}

// ---- Function D (click button D) ----
function funcD() {
    var obj = { name: "test", value: 42 };
    var str = JSON.stringify(obj);
    log("D executed: " + str);
    return str;
}
