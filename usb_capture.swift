// Native macOS CoreMediaIO + AVFoundation USB iPhone screen capture daemon.
// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).

import AVFoundation
import CoreGraphics
import CoreMedia
import CoreMediaIO
import CoreVideo
import Foundation
import ImageIO
import UniformTypeIdentifiers

setbuf(stdout, nil)
setbuf(stderr, nil)

private func enableScreenCaptureDevices() {
    var prop = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain)
    )
    var allow: UInt32 = 1
    let size = UInt32(MemoryLayout<UInt32>.size)
    _ = CMIOObjectSetPropertyData(
        CMIOObjectID(kCMIOObjectSystemObject),
        &prop,
        0,
        nil,
        size,
        &allow
    )
}

private func isIosScreenDevice(_ dev: AVCaptureDevice) -> Bool {
    let name = dev.localizedName.lowercased()
    let model = dev.modelID.lowercased()
    if name.contains("facetime") || name.contains("desk view") {
        return false
    }
    if !dev.hasMediaType(.muxed) && name.contains("camera") && !model.contains("ios") && !model.contains("screencapture") {
        return false
    }
    return model.contains("ios")
        || model.contains("screencapture")
        || name.contains("iphone")
        || name.contains("ipad")
}

private func discoverIosDevices() -> [AVCaptureDevice] {
    var seen = Set<String>()
    var found: [AVCaptureDevice] = []
    let types: [AVCaptureDevice.DeviceType] = [.external, .builtInWideAngleCamera]
    for mediaType in [AVMediaType.muxed, AVMediaType.video] {
        let session = AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: mediaType,
            position: .unspecified
        )
        for dev in session.devices where isIosScreenDevice(dev) {
            if !seen.contains(dev.uniqueID) {
                seen.insert(dev.uniqueID)
                found.append(dev)
            }
        }
    }
    return found
}

private func atomicWriteData(_ data: Data, to path: String, tmpPath: String) {
    do {
        try data.write(to: URL(fileURLWithPath: tmpPath), options: [])
        _ = Darwin.rename(tmpPath, path)
    } catch {
        // Ignore transient write errors
    }
}

private func writeHeartbeat(path: String, active: Bool, w: Int = 0, h: Int = 0, device: String = "") {
    var obj: [String: Any] = [
        "active": active,
        "ts": Date().timeIntervalSince1970,
    ]
    if active {
        obj["w"] = w
        obj["h"] = h
        obj["device"] = device
    }
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []) else { return }
    atomicWriteData(data, to: path, tmpPath: path + ".tmp")
}

private func printUsage() {
    let text = """
    Usage: usb_capture [options]
      --list                   List detected iOS USB screen-capture devices as JSON and exit
      --out <path>             Output JPEG path (default: /tmp/iphone_live.jpg)
      --width <px>             Target scaled width in pixels (default: 720)
      --max-fps <fps>          Maximum JPEG output frame rate (default: 30)
      --heartbeat <path>       Heartbeat JSON path (default: /tmp/iphone_usb_capture.json)
      --udid <id>              Optional AVCaptureDevice uniqueID filter
      --help, -h               Show this help message and exit
    """
    print(text)
}

final class UsbCaptureDaemon: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let outPath: String
    let tmpOutPath: String
    let targetWidth: Int
    let minInterval: Double
    let heartbeatPath: String
    let udidFilter: String?

    private let videoQueue = DispatchQueue(label: "usb_capture.video", qos: .userInteractive)
    private let stateLock = NSLock()
    private var session: AVCaptureSession?
    private var activeDevice: AVCaptureDevice?
    private var lastEmitTime: CFAbsoluteTime = 0
    private var lastFrameWallTime: TimeInterval = 0
    private var lastW: Int = 0
    private var lastH: Int = 0
    private var hotplugTimer: DispatchSourceTimer?
    private var heartbeatTimer: DispatchSourceTimer?

    init(outPath: String, targetWidth: Int, maxFps: Double, heartbeatPath: String, udidFilter: String?) {
        self.outPath = outPath
        self.tmpOutPath = outPath + ".usb.tmp"
        self.targetWidth = max(64, targetWidth)
        self.minInterval = 1.0 / max(1.0, maxFps)
        self.heartbeatPath = heartbeatPath
        self.udidFilter = udidFilter
        super.init()
    }

    func start() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(onDeviceDisconnected(_:)),
            name: AVCaptureDevice.wasDisconnectedNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(onSessionRuntimeError(_:)),
            name: AVCaptureSession.runtimeErrorNotification,
            object: nil
        )

        writeHeartbeat(path: heartbeatPath, active: false)
        tryAttachIfNeeded()

        let hp = DispatchSource.makeTimerSource(queue: .main)
        hp.schedule(deadline: .now() + 1.5, repeating: 1.5)
        hp.setEventHandler { [weak self] in
            self?.tryAttachIfNeeded()
        }
        hp.resume()
        hotplugTimer = hp

        let hb = DispatchSource.makeTimerSource(queue: .main)
        hb.schedule(deadline: .now() + 0.5, repeating: 0.5)
        hb.setEventHandler { [weak self] in
            self?.emitHeartbeatTick()
        }
        hb.resume()
        heartbeatTimer = hb
    }

    func stop() {
        hotplugTimer?.cancel()
        hotplugTimer = nil
        heartbeatTimer?.cancel()
        heartbeatTimer = nil
        teardownSession()
        writeHeartbeat(path: heartbeatPath, active: false)
    }

    @objc private func onDeviceDisconnected(_ note: Notification) {
        guard let dev = note.object as? AVCaptureDevice else { return }
        stateLock.lock()
        let isOurs = activeDevice?.uniqueID == dev.uniqueID
        stateLock.unlock()
        if isOurs {
            teardownSession()
            writeHeartbeat(path: heartbeatPath, active: false)
        }
    }

    @objc private func onSessionRuntimeError(_ note: Notification) {
        teardownSession()
        writeHeartbeat(path: heartbeatPath, active: false)
    }

    private func emitHeartbeatTick() {
        stateLock.lock()
        let devName = activeDevice?.localizedName ?? ""
        let running = session?.isRunning ?? false
        let frameAge = Date().timeIntervalSince1970 - lastFrameWallTime
        let w = lastW
        let h = lastH
        stateLock.unlock()

        if running && lastFrameWallTime > 0 && frameAge < 2.0 && w > 0 && h > 0 {
            writeHeartbeat(path: heartbeatPath, active: true, w: w, h: h, device: devName)
        } else {
            writeHeartbeat(path: heartbeatPath, active: false)
        }
    }

    private func teardownSession() {
        stateLock.lock()
        let s = session
        session = nil
        activeDevice = nil
        lastFrameWallTime = 0
        stateLock.unlock()
        if let s = s, s.isRunning {
            s.stopRunning()
        }
    }

    private func tryAttachIfNeeded() {
        stateLock.lock()
        let alreadyRunning = (session?.isRunning ?? false) && activeDevice != nil
        stateLock.unlock()
        if alreadyRunning { return }

        let devices = discoverIosDevices()
        let chosen: AVCaptureDevice?
        if let filter = udidFilter, !filter.isEmpty {
            chosen = devices.first { $0.uniqueID == filter }
        } else {
            chosen = devices.first
        }
        guard let device = chosen else { return }

        do {
            let sess = AVCaptureSession()
            let input = try AVCaptureDeviceInput(device: device)
            guard sess.canAddInput(input) else { return }
            sess.addInput(input)

            // Note: .inputPriority is unavailable on macOS SDK; setting activeFormat under lockForConfiguration
            // before addOutput + startRunning preserves the highest native resolution.
            try device.lockForConfiguration()
            var bestFormat: AVCaptureDevice.Format?
            var bestPixels: Int64 = -1
            for fmt in device.formats {
                let dims = CMVideoFormatDescriptionGetDimensions(fmt.formatDescription)
                let pixels = Int64(dims.width) * Int64(dims.height)
                if pixels > bestPixels {
                    bestPixels = pixels
                    bestFormat = fmt
                }
            }
            if let best = bestFormat {
                device.activeFormat = best
            }

            let output = AVCaptureVideoDataOutput()
            output.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)
            ]
            output.alwaysDiscardsLateVideoFrames = true
            output.setSampleBufferDelegate(self, queue: videoQueue)
            guard sess.canAddOutput(output) else {
                device.unlockForConfiguration()
                return
            }
            sess.addOutput(output)
            sess.startRunning()
            device.unlockForConfiguration()

            stateLock.lock()
            self.session = sess
            self.activeDevice = device
            self.lastEmitTime = 0
            self.lastFrameWallTime = 0
            stateLock.unlock()
        } catch {
            teardownSession()
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        let nowMono = CFAbsoluteTimeGetCurrent()
        stateLock.lock()
        if nowMono - lastEmitTime < minInterval {
            stateLock.unlock()
            return
        }
        lastEmitTime = nowMono
        stateLock.unlock()

        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }

        let srcW = CVPixelBufferGetWidth(pb)
        let srcH = CVPixelBufferGetHeight(pb)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pb)
        guard srcW > 0, srcH > 0, let base = CVPixelBufferGetBaseAddress(pb) else { return }

        let cs = CGColorSpaceCreateDeviceRGB()
        let bitmapInfo = CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.premultipliedFirst.rawValue
        guard let srcCtx = CGContext(
            data: base,
            width: srcW,
            height: srcH,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: cs,
            bitmapInfo: bitmapInfo
        ), let fullImg = srcCtx.makeImage() else {
            return
        }

        let outW = targetWidth
        let outH = max(1, Int((Double(srcH) * Double(outW) / Double(srcW)).rounded()))
        let emitImg: CGImage
        if outW == srcW && outH == srcH {
            emitImg = fullImg
        } else {
            guard let dstCtx = CGContext(
                data: nil,
                width: outW,
                height: outH,
                bitsPerComponent: 8,
                bytesPerRow: 0,
                space: cs,
                bitmapInfo: bitmapInfo
            ) else {
                return
            }
            dstCtx.interpolationQuality = .medium
            dstCtx.draw(fullImg, in: CGRect(x: 0, y: 0, width: outW, height: outH))
            guard let scaled = dstCtx.makeImage() else { return }
            emitImg = scaled
        }

        let jpegData = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(
            jpegData as CFMutableData,
            UTType.jpeg.identifier as CFString,
            1,
            nil
        ) else {
            return
        }
        let props: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.85]
        CGImageDestinationAddImage(dest, emitImg, props as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return }

        atomicWriteData(jpegData as Data, to: outPath, tmpPath: tmpOutPath)

        stateLock.lock()
        lastW = outW
        lastH = outH
        lastFrameWallTime = Date().timeIntervalSince1970
        let devName = activeDevice?.localizedName ?? ""
        stateLock.unlock()

        if lastFrameWallTime > 0 {
            _ = devName
        }
    }
}

let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--help") || args.contains("-h") {
    printUsage()
    exit(0)
}

enableScreenCaptureDevices()

if args.contains("--list") {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.6))
    let devs = discoverIosDevices().map { dev -> [String: String] in
        [
            "id": dev.uniqueID,
            "name": dev.localizedName,
            "model": dev.modelID,
        ]
    }
    let payload: [String: Any] = ["ok": true, "devices": devs]
    if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
       let json = String(data: data, encoding: .utf8) {
        print(json)
    } else {
        print("{\"ok\":true,\"devices\":[]}")
    }
    exit(0)
}

var outPath = "/tmp/iphone_live.jpg"
var width = 720
var maxFps = 30.0
var heartbeatPath = "/tmp/iphone_usb_capture.json"
var udidFilter: String? = nil

var i = 0
while i < args.count {
    let a = args[i]
    switch a {
    case "--out":
        if i + 1 < args.count { outPath = args[i + 1]; i += 2 } else { i += 1 }
    case "--width":
        if i + 1 < args.count, let v = Int(args[i + 1]) { width = v; i += 2 } else { i += 1 }
    case "--max-fps":
        if i + 1 < args.count, let v = Double(args[i + 1]) { maxFps = v; i += 2 } else { i += 1 }
    case "--heartbeat":
        if i + 1 < args.count { heartbeatPath = args[i + 1]; i += 2 } else { i += 1 }
    case "--udid":
        if i + 1 < args.count { udidFilter = args[i + 1]; i += 2 } else { i += 1 }
    default:
        i += 1
    }
}

let daemon = UsbCaptureDaemon(
    outPath: outPath,
    targetWidth: width,
    maxFps: maxFps,
    heartbeatPath: heartbeatPath,
    udidFilter: udidFilter
)

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigInt.setEventHandler {
    daemon.stop()
    exit(0)
}
sigInt.resume()
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigTerm.setEventHandler {
    daemon.stop()
    exit(0)
}
sigTerm.resume()

daemon.start()
RunLoop.main.run()
