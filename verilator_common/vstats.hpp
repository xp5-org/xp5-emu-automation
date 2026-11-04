// vstats.hpp
// Header-only stats reporter for a Verilator testbench: the "verilator-dev -> manager" half of the API.
// The testbench owns one Stats object and calls set()/add()/tick()/event(); flush() publishes a JSON snapshot
// by write-to-temp + rename(), so a reader (pyhelpers/verilatorhelpers.py StatsReader, the runner's
// /api/v1/instances) never sees a torn file. flush() is rate limited, so calling it every cycle is cheap.
//
// Snapshot schema (schema == 1):
//   { "schema": 1, "name": str, "top": str, "pid": int, "state": "running"|"done"|"failed",
//     "started": epoch_s, "updated": epoch_s, "cycles": int, "sim_time": int (sim units),
//     "counters": {str: int}, "gauges": {str: double}, "info": {str: str},
//     "events": [{"t": epoch_s, "level": "info"|"warn"|"error", "msg": str}, ...]  (last MAX_EVENTS) }
//
// Usage:
//   vstats::Stats st;
//   st.open(vstats::path_from_args(argc, argv), "vga_tb", "vga_hello");   // no-op when no --stats / $VSTATS_PATH
//   st.add("frames");  st.set_gauge("fps", 59.9);  st.tick(cycles, main_time);  st.event("warn", "x");
//   st.finish(ok);                                                        // final flush, state done|failed

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <map>
#include <string>

#include <unistd.h>

namespace vstats {

constexpr int SCHEMA = 1;
constexpr size_t MAX_EVENTS = 50;

// "--stats <path>" from argv, else $VSTATS_PATH, else "" (stats off)
inline std::string path_from_args(int argc, char **argv) {
    for (int i = 1; i + 1 < argc; i++) {
        if (std::strcmp(argv[i], "--stats") == 0) return argv[i + 1];
    }
    const char *env = std::getenv("VSTATS_PATH");
    return env ? env : "";
}

class Stats {
public:
    Stats() : started_(now_()), last_flush_(0), min_interval_(0.25), cycles_(0), sim_time_(0), state_("running") {}
    ~Stats() { if (!path_.empty() && state_ == "running") finish(false); }

    // Returns false when path is empty (stats disabled); every other call is then a cheap no-op.
    bool open(const std::string &path, const std::string &name, const std::string &top = "") {
        path_ = path;
        name_ = name;
        top_ = top;
        if (path_.empty()) return false;
        flush(true);
        return true;
    }

    bool enabled() const { return !path_.empty(); }
    void set_interval(double seconds) { min_interval_ = seconds; }

    void add(const std::string &k, int64_t n = 1) { counters_[k] += n; maybe_flush(); }
    void set_counter(const std::string &k, int64_t v) { counters_[k] = v; maybe_flush(); }
    void set_gauge(const std::string &k, double v) { gauges_[k] = v; maybe_flush(); }
    void info(const std::string &k, const std::string &v) { info_[k] = v; }

    void tick(int64_t cycles, int64_t sim_time) { cycles_ = cycles; sim_time_ = sim_time; maybe_flush(); }

    void event(const std::string &level, const std::string &msg) {
        events_.push_back({now_(), level, msg});
        if (events_.size() > MAX_EVENTS) events_.pop_front();
        flush(true);
    }

    void finish(bool ok) {
        state_ = ok ? "done" : "failed";
        flush(true);
    }

    void flush(bool force = false) {
        if (path_.empty()) return;
        double t = now_();
        if (!force && t - last_flush_ < min_interval_) return;
        last_flush_ = t;
        std::string tmp = path_ + ".tmp";
        FILE *f = std::fopen(tmp.c_str(), "w");
        if (!f) return;
        std::string j = json_(t);
        std::fwrite(j.data(), 1, j.size(), f);
        std::fclose(f);
        std::rename(tmp.c_str(), path_.c_str());
    }

private:
    struct Event { double t; std::string level, msg; };

    static double now_() {
        timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        return ts.tv_sec + ts.tv_nsec / 1e9;
    }

    void maybe_flush() { flush(false); }

    static std::string esc_(const std::string &s) {
        std::string o;
        for (unsigned char c : s) {
            if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
            else if (c == '\n') o += "\\n";
            else if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
            else o += (char)c;
        }
        return o;
    }

    std::string json_(double t) const {
        char b[96];
        std::string j = "{\"schema\":" + std::to_string(SCHEMA);
        j += ",\"name\":\"" + esc_(name_) + "\",\"top\":\"" + esc_(top_) + "\"";
        j += ",\"pid\":" + std::to_string((long)getpid());
        j += ",\"state\":\"" + state_ + "\"";
        std::snprintf(b, sizeof b, ",\"started\":%.3f,\"updated\":%.3f", started_, t);
        j += b;
        j += ",\"cycles\":" + std::to_string((long long)cycles_);
        j += ",\"sim_time\":" + std::to_string((long long)sim_time_);
        j += ",\"counters\":{";
        bool first = true;
        for (auto &kv : counters_) {
            j += (first ? "" : ",") + std::string("\"") + esc_(kv.first) + "\":" + std::to_string((long long)kv.second);
            first = false;
        }
        j += "},\"gauges\":{";
        first = true;
        for (auto &kv : gauges_) {
            std::snprintf(b, sizeof b, "%.9g", kv.second);
            j += (first ? "" : ",") + std::string("\"") + esc_(kv.first) + "\":" + b;
            first = false;
        }
        j += "},\"info\":{";
        first = true;
        for (auto &kv : info_) {
            j += (first ? "" : ",") + std::string("\"") + esc_(kv.first) + "\":\"" + esc_(kv.second) + "\"";
            first = false;
        }
        j += "},\"events\":[";
        first = true;
        for (auto &e : events_) {
            std::snprintf(b, sizeof b, "{\"t\":%.3f,\"level\":\"", e.t);
            j += (first ? "" : ",") + std::string(b) + esc_(e.level) + "\",\"msg\":\"" + esc_(e.msg) + "\"}";
            first = false;
        }
        j += "]}\n";
        return j;
    }

    std::string path_, name_, top_;
    double started_, last_flush_, min_interval_;
    int64_t cycles_, sim_time_;
    std::string state_;
    std::map<std::string, int64_t> counters_;
    std::map<std::string, double> gauges_;
    std::map<std::string, std::string> info_;
    std::deque<Event> events_;
};

}  // namespace vstats
