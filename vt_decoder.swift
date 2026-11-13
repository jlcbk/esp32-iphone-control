// Hardware-accelerated macOS VideoToolbox H.264 -> live JPEG decoder for ESP32 AirPlay stream.
// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).

import CoreMedia
import Foundation
import ImageIO
import UniformTypeIdentifiers
import VideoToolbox

func annexBUnits(_ data: Data) -> [Data] {
    return data.withUnsafeBytes { raw -> [Data] in
        guard let b = raw.bindMemory(to: UInt8.self).baseAddress else { return [] }
        let count = raw.count
        var starts: [(Int, Int)] = []
        var i = 0
        while i + 2 < count {
            if b[i] == 0 && b[i + 1] == 0 && b[i + 2] == 1 {
                let code = (i > 0 && b[i - 1] == 0) ? (i - 1) : i
                starts.append((code, i + 3))
                i += 3
            } else {
                i += 1
            }
        }
        var out: [Data] = []
        out.reserveCapacity(starts.count)
        for (idx, s) in starts.enumerated() {
            let end = (idx + 1 < starts.count) ? starts[idx + 1].0 : count
            if end > s.1 {
                out.append(Data(bytes: b.advanced(by: s.1), count: end - s.1))
            }
        }
        return out
    }
}

final class LiveVTDecoder {
    let outPath: String
    let tmpPath: String
    var sps: Data?
    var pps: Data?
    var format: CMVideoFormatDescription?
    var session: VTDecompressionSession?
    var lastEmit: CFAbsoluteTime = 0
    let minGap: Double = 0.08

    init(outPath: String) {
        self.outPath = outPath
        self.tmpPath = outPath + ".vt.tmp.jpg"
    }

    func resetSession() {
        if let s = session {
            VTDecompressionSessionWaitForAsynchronousFrames(s)
            VTDecompressionSessionInvalidate(s)
        }
        session = nil
        format = nil
    }

    func ensureSession() -> Bool {
        if session != nil { return true }
        guard let sps = sps, let pps = pps else { return false }
        var desc: CMFormatDescription?
        let ok = sps.withUnsafeBytes { sRaw in
            pps.withUnsafeBytes { pRaw in
                let ptrs = [
                    sRaw.bindMemory(to: UInt8.self).baseAddress!,
                    pRaw.bindMemory(to: UInt8.self).baseAddress!
                ]
                let sizes = [sps.count, pps.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: nil,
                    parameterSetCount: 2,
                    parameterSetPointers: ptrs,
                    parameterSetSizes: sizes,
                    nalUnitHeaderLength: 4,
                    formatDescriptionOut: &desc
                )
            }
        }
        guard ok == noErr, let validDesc = desc else { return false }
        let attrs: [CFString: Any] = [kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA]
        var made: VTDecompressionSession?
        let st = VTDecompressionSessionCreate(
            allocator: nil,
            formatDescription: validDesc,
            decoderSpecification: nil,
            imageBufferAttributes: attrs as CFDictionary,
            outputCallback: nil,
            decompressionSessionOut: &made
        )
        guard st == noErr, let validSession = made else { return false }
        format = validDesc
        session = validSession
        return true
    }

    func decodeSlices(_ slices: [Data], emit: Bool, forceEmit: Bool = false) {
        guard !slices.isEmpty, ensureSession(), let session = session, let format = format else { return }
        var avcc = Data()
        for s in slices {
            var l = UInt32(s.count).bigEndian
            withUnsafeBytes(of: &l) { avcc.append(contentsOf: $0) }
            avcc.append(s)
        }
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: nil, memoryBlock: nil, blockLength: avcc.count,
            blockAllocator: nil, customBlockSource: nil, offsetToData: 0,
            dataLength: avcc.count, flags: 0, blockBufferOut: &block
        ) == noErr, let validBlock = block else { return }

        let copied = avcc.withUnsafeBytes { raw in
            CMBlockBufferReplaceDataBytes(
                with: raw.baseAddress!, blockBuffer: validBlock,
                offsetIntoDestination: 0, dataLength: avcc.count
            )
        }
        guard copied == noErr else { return }

        var sample: CMSampleBuffer?
        var size = avcc.count
        guard CMSampleBufferCreateReady(
            allocator: nil, dataBuffer: validBlock, formatDescription: format,
            sampleCount: 1, sampleTimingEntryCount: 0, sampleTimingArray: nil,
            sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample
        ) == noErr, let validSample = sample else { return }

        var flags = VTDecodeInfoFlags()
        _ = VTDecompressionSessionDecodeFrame(
            session, sampleBuffer: validSample, flags: [], infoFlagsOut: &flags
        ) { [weak self] status, _, imageBuffer, _, _ in
            guard let self = self, emit else { return }
            guard status == noErr, let buf = imageBuffer else {
                fputs("[vt_decoder] decode status=\(status)\n", stderr)
                return
            }
            let now = CFAbsoluteTimeGetCurrent()
            if !forceEmit && (now - self.lastEmit) < self.minGap { return }
            self.lastEmit = now
            self.writeJPEG(buf)
        }
    }

    func feedAccessUnit(_ auData: Data, emit: Bool, forceEmit: Bool = false) {
        var slices: [Data] = []
        for nal in annexBUnits(auData) {
            guard !nal.isEmpty else { continue }
            let nalType = nal[nal.startIndex] & 0x1f
            switch nalType {
            case 7:
                if nal != sps { sps = nal; resetSession() }
            case 8:
                if nal != pps { pps = nal; resetSession() }
            case 6, 9:
                break
            case 1, 5:
                slices.append(nal)
            default:
                break
            }
        }
        if !slices.isEmpty {
            decodeSlices(slices, emit: emit, forceEmit: forceEmit)
        }
    }

    func warmupFromConcatenatedAnnexB(_ path: String) {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)), !data.isEmpty else { return }
        let nals = annexBUnits(data)
        var frames: [[Data]] = []
        var curSlices: [Data] = []

        for nal in nals {
            guard !nal.isEmpty else { continue }
            let nalType = nal[nal.startIndex] & 0x1f
            if nalType == 7 {
                if !curSlices.isEmpty { frames.append(curSlices); curSlices = [] }
                if nal != sps { sps = nal; resetSession() }
            } else if nalType == 8 {
                if !curSlices.isEmpty { frames.append(curSlices); curSlices = [] }
                if nal != pps { pps = nal; resetSession() }
            } else if nalType == 1 || nalType == 5 {
                // Bit 7 of slice header is 1 iff first_mb_in_slice == 0 (Exp-Golomb ue(v))
                let isFirstSlice = nal.count >= 2 && ((nal[nal.startIndex + 1] & 0x80) != 0)
                if isFirstSlice && !curSlices.isEmpty {
                    frames.append(curSlices)
                    curSlices = []
                }
                curSlices.append(nal)
            }
        }
        if !curSlices.isEmpty { frames.append(curSlices) }

        var firstErrLogged = false
        for (i, slices) in frames.enumerated() {
            let nearEnd = (i >= frames.count - 1)
            guard !slices.isEmpty, ensureSession(), let session = session, let format = format else { continue }
            var avcc = Data()
            for s in slices {
                var l = UInt32(s.count).bigEndian
                withUnsafeBytes(of: &l) { avcc.append(contentsOf: $0) }
                avcc.append(s)
            }
            var block: CMBlockBuffer?
            guard CMBlockBufferCreateWithMemoryBlock(
                allocator: nil, memoryBlock: nil, blockLength: avcc.count,
                blockAllocator: nil, customBlockSource: nil, offsetToData: 0,
                dataLength: avcc.count, flags: 0, blockBufferOut: &block
            ) == noErr, let validBlock = block else { continue }
            _ = avcc.withUnsafeBytes { raw in
                CMBlockBufferReplaceDataBytes(with: raw.baseAddress!, blockBuffer: validBlock, offsetIntoDestination: 0, dataLength: avcc.count)
            }
            var sample: CMSampleBuffer?
            var size = avcc.count
            guard CMSampleBufferCreateReady(
                allocator: nil, dataBuffer: validBlock, formatDescription: format,
                sampleCount: 1, sampleTimingEntryCount: 0, sampleTimingArray: nil,
                sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample
            ) == noErr, let validSample = sample else { continue }
            var flags = VTDecodeInfoFlags()
            let ret = VTDecompressionSessionDecodeFrame(session, sampleBuffer: validSample, flags: [], infoFlagsOut: &flags) { [weak self] st, _, img, _, _ in
                if st != noErr && !firstErrLogged {
                    firstErrLogged = true
                    fputs("[vt_decoder] FIRST callback err at frame \(i): st=\(st), sliceCount=\(slices.count), len0=\(slices[0].count), hdr=\(String(format: "%02x%02x", slices[0][0], slices[0][1]))\n", stderr)
                }
                if let self = self, nearEnd, st == noErr, let buf = img {
                    self.writeJPEG(buf)
                }
            }
            if ret != noErr && !firstErrLogged {
                firstErrLogged = true
                fputs("[vt_decoder] FIRST call err at frame \(i): ret=\(ret)\n", stderr)
            }
        }
        if let s = session {
            VTDecompressionSessionWaitForAsynchronousFrames(s)
        }
        fputs("[vt_decoder] Warmed up \(frames.count) frames from \(path)\n", stderr)
    }

    func writeJPEG(_ pixelBuffer: CVImageBuffer) {
        var cgImage: CGImage?
        VTCreateCGImageFromCVPixelBuffer(pixelBuffer, options: nil, imageOut: &cgImage)
        guard let image = cgImage else { return }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.82] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return }
        do {
            try (out as Data).write(to: URL(fileURLWithPath: tmpPath), options: .atomic)
            _ = rename(tmpPath, outPath)
        } catch {}
    }
}

func readExact(_ fd: Int32, _ buf: UnsafeMutableRawPointer, _ count: Int) -> Bool {
    var got = 0
    while got < count {
        let n = Darwin.read(fd, buf.advanced(by: got), count - got)
        if n <= 0 { return false }
        got += n
    }
    return true
}

let args = CommandLine.arguments
var outPath = "/tmp/iphone_live.jpg"
var warmupPath: String? = nil
var idx = 1
while idx < args.count {
    if args[idx] == "--out", idx + 1 < args.count {
        outPath = args[idx + 1]
        idx += 2
    } else if args[idx] == "--warmup", idx + 1 < args.count {
        warmupPath = args[idx + 1]
        idx += 2
    } else {
        idx += 1
    }
}

let decoder = LiveVTDecoder(outPath: outPath)
if let wp = warmupPath {
    decoder.warmupFromConcatenatedAnnexB(wp)
}

var lenLE: UInt32 = 0
while readExact(STDIN_FILENO, &lenLE, 4) {
    let n = Int(UInt32(littleEndian: lenLE))
    if n == 0 {
        decoder.resetSession()
        continue
    }
    if n > 16 * 1024 * 1024 { break }
    var buf = Data(count: n)
    let ok = buf.withUnsafeMutableBytes { raw in
        readExact(STDIN_FILENO, raw.baseAddress!, n)
    }
    if !ok { break }
    decoder.feedAccessUnit(buf, emit: true, forceEmit: true)
}
