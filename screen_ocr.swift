// Native Apple Silicon Vision OCR (1-shot CLI + persistent `--serve` streaming mode).
// Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
// Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).

import Foundation
import Vision
import ImageIO

setbuf(stdout, nil)

final class OCRRunner {
    var curW: Double = 720.0
    var curH: Double = 1558.0
    var items: [[String: Any]] = []
    lazy var req: VNRecognizeTextRequest = {
        let r = VNRecognizeTextRequest { [unowned self] request, _ in
            guard let results = request.results as? [VNRecognizedTextObservation] else { return }
            var out: [[String: Any]] = []
            out.reserveCapacity(results.count)
            for obs in results {
                if let top = obs.topCandidates(1).first {
                    let box = obs.boundingBox
                    let cx = Int((box.origin.x + box.size.width / 2.0) * self.curW)
                    let cy = Int((1.0 - (box.origin.y + box.size.height / 2.0)) * self.curH)
                    let bw = Int(box.size.width * self.curW)
                    let bh = Int(box.size.height * self.curH)
                    out.append(["text": top.string, "x": cx, "y": cy, "w": bw, "h": bh])
                }
            }
            self.items = out
        }
        r.recognitionLevel = .accurate
        r.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
        r.usesLanguageCorrection = !self.noCorr
        return r
    }()
    let noCorr: Bool

    init(noCorr: Bool) {
        self.noCorr = noCorr
    }

    func recognize(_ path: String) -> String {
        let url = URL(fileURLWithPath: path) as CFURL
        guard let src = CGImageSourceCreateWithURL(url, nil),
              let cgImg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            return "[]"
        }
        curW = Double(cgImg.width)
        curH = Double(cgImg.height)
        items = []
        let handler = VNImageRequestHandler(cgImage: cgImg, options: [:])
        try? handler.perform([req])
        if let data = try? JSONSerialization.data(withJSONObject: items, options: []),
           let str = String(data: data, encoding: .utf8) {
            return str
        }
        return "[]"
    }
}

let args = CommandLine.arguments
let noCorr = args.contains("--no-corr")
let runner = OCRRunner(noCorr: noCorr)

if args.count > 1 && args[1] == "--serve" {
    while let line = readLine() {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }
        if trimmed == "QUIT" { break }
        print(runner.recognize(trimmed))
    }
} else if args.count > 1 {
    print(runner.recognize(args[1]))
} else {
    exit(1)
}
