// live_sink.hpp 
// shared-memory live view sink for a Verilator testbench.

#pragma once

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace livesink {

constexpr uint32_t MAGIC = 0x42464C56;  // "VLFB" little-endian, see header comment above
constexpr uint32_t VERSION = 1;
constexpr uint32_t FMT_RGB1 = 1;  // 1 byte/pixel: bit2=R, bit1=G, bit0=B
constexpr uint32_t FMT_RGB565 = 2;  // 2 bytes/pixel little-endian: R[15:11] G[10:5] B[4:0]

constexpr size_t HDR_WORDS = 16;
constexpr size_t HDR_BYTES = HDR_WORDS * 4;  // 64
constexpr size_t MAX_PAYLOAD = 1024 * 1024 * 4;  // 1024x1024 at 32bpp headroom
constexpr size_t FILE_BYTES = HDR_BYTES + MAX_PAYLOAD;

// Header word indices, matching verilatorfbhelpers.py's struct.
enum HdrWord {
    H_MAGIC = 0, H_VERSION, H_FORMAT, H_WIDTH, H_HEIGHT,
    H_STRIDE, H_BYTES, H_SEQ, H_FRAME
};

class LiveSink {
public:
    LiveSink() : fd_(-1), map_(nullptr), seq_(0), frame_(0) {}
    ~LiveSink() { close(); }

    bool open(const std::string &path) {
        close();
        fd_ = ::open(path.c_str(), O_RDWR | O_CREAT, 0644);
        if (fd_ < 0) return false;
        if (ftruncate(fd_, FILE_BYTES) != 0) {
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        void *m = mmap(nullptr, FILE_BYTES, PROT_READ | PROT_WRITE,
                       MAP_SHARED, fd_, 0);
        if (m == MAP_FAILED) {
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        map_ = static_cast<uint8_t *>(m);
        std::memset(map_, 0, HDR_BYTES);
        return true;
    }

    void close() {
        if (map_) {
            munmap(map_, FILE_BYTES);
            map_ = nullptr;
        }
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
    }

    bool is_open() const { return map_ != nullptr; }

    // width/height/stride/format describe pixels; nbytes must be <= MAX_PAYLOAD.
    // Odd SEQ marks "writer mid-copy" for the reader
    void publish(uint32_t width, uint32_t height, uint32_t stride,
                uint32_t format, const uint8_t *pixels, size_t nbytes) {
        if (!map_ || nbytes > MAX_PAYLOAD) return;
        auto *hdr = reinterpret_cast<uint32_t *>(map_);

        seq_ += 1;  // now odd: mid-write
        hdr[H_SEQ] = seq_;
        __sync_synchronize();

        std::memcpy(map_ + HDR_BYTES, pixels, nbytes);

        frame_ += 1;
        hdr[H_MAGIC] = MAGIC;
        hdr[H_VERSION] = VERSION;
        hdr[H_FORMAT] = format;
        hdr[H_WIDTH] = width;
        hdr[H_HEIGHT] = height;
        hdr[H_STRIDE] = stride;
        hdr[H_BYTES] = static_cast<uint32_t>(nbytes);
        hdr[H_FRAME] = frame_;

        __sync_synchronize();
        seq_ += 1;  // now even: whole frame visible
        hdr[H_SEQ] = seq_;
    }

private:
    int fd_;
    uint8_t *map_;
    uint32_t seq_;
    uint32_t frame_;
};

}  // namespace livesink
