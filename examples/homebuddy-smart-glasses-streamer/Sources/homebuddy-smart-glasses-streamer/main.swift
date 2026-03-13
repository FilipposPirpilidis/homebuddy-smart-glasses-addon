@preconcurrency import AVFoundation
import AudioToolbox
import Carbon.HIToolbox
import CoreGraphics
import Foundation

struct AudioConfig {
    let rate: Double
    let channels: AVAudioChannelCount
    let blockSize: AVAudioFrameCount
    let width: Int
    let codec: String

    static func make(codec: String) -> AudioConfig {
        let normalized = codec.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized == "opus" {
            return AudioConfig(rate: 16_000, channels: 1, blockSize: 960, width: 2, codec: "opus")
        }
        return AudioConfig(rate: 16_000, channels: 1, blockSize: 1_024, width: 2, codec: "pcm16")
    }
}

struct CLIOptions {
    var scheme: String = "ws"
    var host: String = "homeassistant.local"
    var port: Int = 8123
    var codec: String = "pcm16"
    var language: String = "en"
    var mode: String = "transcription"
    var haToken: String? = ProcessInfo.processInfo.environment["HA_TOKEN"]

    static func parse(from args: [String]) throws -> CLIOptions {
        var options = CLIOptions()
        var index = 0

        while index < args.count {
            let arg = args[index]
            switch arg {
            case "--scheme":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--scheme") }
                options.scheme = args[index]
            case "--host":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--host") }
                options.host = args[index]
            case "--codec":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--codec") }
                options.codec = args[index]
            case "--port":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--port") }
                guard let port = Int(args[index]) else { throw CLIError.invalidValue("--port") }
                options.port = port
            case "--language":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--language") }
                options.language = args[index]
            case "--mode":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--mode") }
                let mode = args[index].trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                guard mode == "transcription" || mode == "agent" else { throw CLIError.invalidValue("--mode") }
                options.mode = mode
            case "--ha-token":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--ha-token") }
                options.haToken = args[index]
            case "-h", "--help":
                printUsageAndExit()
            default:
                throw CLIError.unknownArgument(arg)
            }
            index += 1
        }

        return options
    }
}

enum CLIError: Error, CustomStringConvertible {
    case missingValue(String)
    case invalidValue(String)
    case unknownArgument(String)

    var description: String {
        switch self {
        case let .missingValue(flag):
            return "Missing value for \(flag)"
        case let .invalidValue(flag):
            return "Invalid value for \(flag)"
        case let .unknownArgument(arg):
            return "Unknown argument: \(arg)"
        }
    }
}

enum AppError: Error, CustomStringConvertible {
    case microphoneAccessDenied
    case failedToCreateAudioFormat
    case failedToCreateConverter
    case failedToStartAudioEngine(Error)
    case failedToCreateEventTap
    case streamConnectionFailed
    case malformedHeader
    case invalidJSON
    case emptyHost
    case missingHAToken
    case invalidWebSocketURL
    case authRequired
    case authenticationFailed
    case missingSessionID

    var description: String {
        switch self {
        case .microphoneAccessDenied:
            return "Microphone access denied"
        case .failedToCreateAudioFormat:
            return "Failed to create audio format"
        case .failedToCreateConverter:
            return "Failed to create audio converter"
        case let .failedToStartAudioEngine(error):
            return "Failed to start audio engine: \(error.localizedDescription)"
        case .failedToCreateEventTap:
            return "Failed to create keyboard event tap (enable Accessibility permissions for this app or terminal)"
        case .streamConnectionFailed:
            return "Failed to open socket streams"
        case .malformedHeader:
            return "Malformed Wyoming header"
        case .invalidJSON:
            return "Invalid JSON received from server"
        case .emptyHost:
            return "Host cannot be empty"
        case .missingHAToken:
            return "Missing Home Assistant token"
        case .invalidWebSocketURL:
            return "Invalid WebSocket URL"
        case .authRequired:
            return "Home Assistant authentication handshake failed"
        case .authenticationFailed:
            return "Home Assistant authentication was rejected"
        case .missingSessionID:
            return "Home Assistant did not return a session_id"
        }
    }
}

final class ConverterInputState: @unchecked Sendable {
    var supplied = false
}

final class OpusPacketEncoder {
    private let pcmFormat: AVAudioFormat
    private let converter: AVAudioConverter
    private let frameSize: AVAudioFrameCount = 320
    private let bytesPerFrame = 2
    private var pendingPCM = Data()

    init(pcmFormat: AVAudioFormat) throws {
        self.pcmFormat = pcmFormat
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatOpus,
            AVSampleRateKey: pcmFormat.sampleRate,
            AVNumberOfChannelsKey: Int(pcmFormat.channelCount)
        ]
        guard let opusFormat = AVAudioFormat(settings: settings) else {
            throw AppError.failedToCreateAudioFormat
        }
        guard let converter = AVAudioConverter(from: pcmFormat, to: opusFormat) else {
            throw AppError.failedToCreateConverter
        }
        converter.bitRate = 16_000
        self.converter = converter
    }

    func encodePCM(_ data: Data) throws -> [Data] {
        pendingPCM.append(data)
        var packets: [Data] = []
        let chunkByteCount = Int(frameSize) * bytesPerFrame
        while pendingPCM.count >= chunkByteCount {
            let chunk = pendingPCM.prefix(chunkByteCount)
            pendingPCM.removeFirst(chunkByteCount)
            if let packet = try encodeChunk(Data(chunk)) {
                packets.append(packet)
            }
        }
        return packets
    }

    func flush() throws -> [Data] {
        guard !pendingPCM.isEmpty else {
            return []
        }
        let chunkByteCount = Int(frameSize) * bytesPerFrame
        if pendingPCM.count < chunkByteCount {
            pendingPCM.append(Data(count: chunkByteCount - pendingPCM.count))
        }
        let chunk = pendingPCM
        pendingPCM.removeAll(keepingCapacity: false)
        if let packet = try encodeChunk(chunk) {
            return [packet]
        }
        return []
    }

    private func encodeChunk(_ pcm: Data) throws -> Data? {
        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: pcmFormat, frameCapacity: frameSize),
              let channelData = pcmBuffer.int16ChannelData?.pointee else {
            throw AppError.failedToCreateAudioFormat
        }

        pcm.copyBytes(to: UnsafeMutableRawBufferPointer(start: channelData, count: pcm.count))
        pcmBuffer.frameLength = frameSize

        let maxPacketSize = max(converter.maximumOutputPacketSize, 512)
        let compressed = AVAudioCompressedBuffer(
            format: converter.outputFormat,
            packetCapacity: 1,
            maximumPacketSize: maxPacketSize
        )

        let inputState = ConverterInputState()
        var conversionError: NSError?
        let status = converter.convert(to: compressed, error: &conversionError) { _, outStatus in
            if inputState.supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            inputState.supplied = true
            outStatus.pointee = .haveData
            return pcmBuffer
        }

        if status == .error || conversionError != nil {
            throw conversionError ?? AppError.failedToCreateConverter
        }
        guard compressed.byteLength > 0 else {
            return nil
        }
        return Data(bytes: compressed.data, count: Int(compressed.byteLength))
    }
}

final class PushToTalkRecorder {
    private let cfg: AudioConfig
    private let engine = AVAudioEngine()
    private let inputFormat: AVAudioFormat
    private let targetFormat: AVAudioFormat
    private let converter: AVAudioConverter
    private let stateQueue = DispatchQueue(label: "homebuddy.smartglasses.recorder.state")
    private let opusEncoder: OpusPacketEncoder?

    private var recording = false
    private var recordedBytes = 0
    private var chunkHandler: ((Data) -> Void)?

    init(cfg: AudioConfig) throws {
        self.cfg = cfg
        inputFormat = engine.inputNode.inputFormat(forBus: 0)

        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: cfg.rate,
            channels: cfg.channels,
            interleaved: true
        ) else {
            throw AppError.failedToCreateAudioFormat
        }
        self.targetFormat = targetFormat

        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw AppError.failedToCreateConverter
        }
        self.converter = converter
        self.opusEncoder = cfg.codec == "opus" ? try OpusPacketEncoder(pcmFormat: targetFormat) : nil

        engine.inputNode.installTap(onBus: 0, bufferSize: cfg.blockSize, format: inputFormat) { [weak self] buffer, _ in
            self?.handleInput(buffer: buffer)
        }

        do {
            try engine.start()
        } catch {
            throw AppError.failedToStartAudioEngine(error)
        }
    }

    deinit {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    func start(chunkHandler: @escaping (Data) -> Void) {
        stateQueue.sync {
            recordedBytes = 0
            self.chunkHandler = chunkHandler
            recording = true
        }
    }

    func stop() -> Int {
        stateQueue.sync {
            let callback = chunkHandler
            if let opusEncoder, let callback {
                let flushedPackets = (try? opusEncoder.flush()) ?? []
                for packet in flushedPackets {
                    callback(packet)
                }
            }
            recording = false
            chunkHandler = nil
            let durationMs = Int((Double(recordedBytes) / (Double(cfg.width) * Double(cfg.channels) * cfg.rate)) * 1000.0)
            recordedBytes = 0
            return durationMs
        }
    }

    private func handleInput(buffer: AVAudioPCMBuffer) {
        let callback = stateQueue.sync { recording ? chunkHandler : nil }
        guard let callback else { return }

        let ratio = targetFormat.sampleRate / max(1.0, inputFormat.sampleRate)
        let outputCapacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outputCapacity) else {
            return
        }

        let inputState = ConverterInputState()
        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError) { _, outStatus in
            if inputState.supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            inputState.supplied = true
            outStatus.pointee = .haveData
            return buffer
        }

        if status == .error || conversionError != nil {
            return
        }

        guard outputBuffer.frameLength > 0,
              let channelData = outputBuffer.int16ChannelData else {
            return
        }

        let byteCount = Int(outputBuffer.frameLength) * Int(targetFormat.streamDescription.pointee.mBytesPerFrame)
        let data = Data(bytes: channelData.pointee, count: byteCount)

        stateQueue.sync {
            guard recording else { return }
            recordedBytes += data.count
        }
        if let opusEncoder {
            do {
                let packets = try opusEncoder.encodePCM(data)
                for packet in packets {
                    callback(packet)
                }
            } catch {
                return
            }
        } else {
            callback(data)
        }
    }
}

final class KeyboardMonitor {
    var onEscDown: (() -> Void)?

    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?

    func start() throws {
        let mask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)

        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else {
                return Unmanaged.passUnretained(event)
            }

            let monitor = Unmanaged<KeyboardMonitor>.fromOpaque(userInfo).takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = monitor.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            let keyCode = Int(event.getIntegerValueField(.keyboardEventKeycode))

            if type == .keyDown {
                if keyCode == kVK_Escape {
                    monitor.onEscDown?()
                }
            }

            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: CGEventMask(mask),
            callback: callback,
            userInfo: UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())
        ) else {
            throw AppError.failedToCreateEventTap
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        if let runLoopSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        }
        CGEvent.tapEnable(tap: tap, enable: true)
    }

    func stop() {
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        runLoopSource = nil
        eventTap = nil
    }
}

func eventBytes(type: String, data: [String: Any]? = nil, payload: Data = Data()) throws -> Data {
    var header: [String: Any] = ["type": type]
    if let data, !data.isEmpty {
        header["data"] = data
    }
    if !payload.isEmpty {
        header["payload_length"] = payload.count
    }

    let json = try JSONSerialization.data(withJSONObject: header)
    guard var line = String(data: json, encoding: .utf8)?.data(using: .utf8) else {
        throw AppError.invalidJSON
    }
    line.append(0x0A)

    var output = Data()
    output.append(line)
    output.append(payload)
    return output
}

func readLine(stream: InputStream) throws -> Data {
    var line = Data()
    var byte: UInt8 = 0

    while true {
        let count = stream.read(&byte, maxLength: 1)
        if count < 0 {
            throw stream.streamError ?? AppError.streamConnectionFailed
        }
        if count == 0 {
            if line.isEmpty {
                throw AppError.malformedHeader
            }
            break
        }

        line.append(byte)
        if byte == 0x0A {
            break
        }
    }

    return line
}

func readExact(stream: InputStream, byteCount: Int) throws -> Data {
    var out = Data(count: byteCount)
    var offset = 0

    try out.withUnsafeMutableBytes { rawBuffer in
        guard let base = rawBuffer.bindMemory(to: UInt8.self).baseAddress else {
            throw AppError.streamConnectionFailed
        }

        while offset < byteCount {
            let readCount = stream.read(base.advanced(by: offset), maxLength: byteCount - offset)
            if readCount < 0 {
                throw stream.streamError ?? AppError.streamConnectionFailed
            }
            if readCount == 0 {
                throw AppError.streamConnectionFailed
            }
            offset += readCount
        }
    }

    return out
}

func intValue(_ value: Any?) -> Int {
    if let int = value as? Int { return int }
    if let number = value as? NSNumber { return number.intValue }
    if let str = value as? String, let int = Int(str) { return int }
    return 0
}

func readEvent(stream: InputStream) throws -> ([String: Any], Data) {
    let lineData = try readLine(stream: stream)
    guard let lineText = String(data: lineData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
          let headerData = lineText.data(using: .utf8),
          var header = try JSONSerialization.jsonObject(with: headerData) as? [String: Any] else {
        throw AppError.invalidJSON
    }

    var data = header["data"] as? [String: Any] ?? [:]

    let dataLength = intValue(header["data_length"])
    if dataLength > 0 {
        let extra = try readExact(stream: stream, byteCount: dataLength)
        if let extraJSON = try JSONSerialization.jsonObject(with: extra) as? [String: Any] {
            for (key, value) in extraJSON {
                data[key] = value
            }
        }
    }

    let payloadLength = intValue(header["payload_length"])
    let payload = payloadLength > 0 ? try readExact(stream: stream, byteCount: payloadLength) : Data()

    header["data"] = data
    return (header, payload)
}

func writeAll(stream: OutputStream, data: Data) throws {
    try data.withUnsafeBytes { rawBuffer in
        guard let base = rawBuffer.bindMemory(to: UInt8.self).baseAddress else {
            throw AppError.streamConnectionFailed
        }

        var offset = 0
        while offset < data.count {
            let written = stream.write(base.advanced(by: offset), maxLength: data.count - offset)
            if written < 0 {
                throw stream.streamError ?? AppError.streamConnectionFailed
            }
            if written == 0 {
                throw AppError.streamConnectionFailed
            }
            offset += written
        }
    }
}

final class PendingResult: @unchecked Sendable {
    let semaphore = DispatchSemaphore(value: 0)
    var payload: [String: Any]?
    var error: Error?
}

final class AppState: @unchecked Sendable {
    var client: HomeAssistantWebSocketClient?
}

final class HomeAssistantWebSocketClient: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    var onPartialTranscript: ((String) -> Void)?
    var onFinalTranscript: ((String) -> Void)?
    var onAgentResponse: ((String, String) -> Void)?
    var onBackendMode: ((String) -> Void)?
    var onError: ((Error) -> Void)?

    private let scheme: String
    private let host: String
    private let port: Int
    private let codec: String
    private let haToken: String
    private let language: String
    private let mode: String
    private let cfg: AudioConfig
    private let writeQueue = DispatchQueue(label: "homebuddy.smartglasses.write")
    private let stateQueue = DispatchQueue(label: "homebuddy.smartglasses.state")

    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var closed = false
    private var nextMessageID = 1
    private var sessionID: String?
    private var pendingResults: [Int: PendingResult] = [:]

    init(scheme: String, host: String, port: Int, codec: String, haToken: String, language: String, mode: String, cfg: AudioConfig) {
        self.scheme = scheme
        self.host = host
        self.port = port
        self.codec = codec
        self.haToken = haToken
        self.language = language
        self.mode = mode
        self.cfg = cfg
    }

    func start() throws {
        guard !host.isEmpty else {
            throw AppError.emptyHost
        }
        guard !haToken.isEmpty else {
            throw AppError.missingHAToken
        }
        guard let url = URL(string: "\(scheme)://\(host):\(port)/api/websocket") else {
            throw AppError.invalidWebSocketURL
        }

        let configuration = URLSessionConfiguration.default
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        try authenticate()
        readLoop()
        let response = try sendCommandSync(
            type: "homebuddy_smart_glasses_service/open_stream",
            payload: [
                "mode": mode,
                "language": language,
                "codec": codec,
                "rate": Int(cfg.rate),
                "width": cfg.width,
                "channels": Int(cfg.channels)
            ]
        )
        guard let sessionID = response["session_id"] as? String, !sessionID.isEmpty else {
            throw AppError.missingSessionID
        }
        if let backendMode = response["backend_mode"] as? String, !backendMode.isEmpty {
            onBackendMode?(backendMode)
        }
        stateQueue.sync {
            self.sessionID = sessionID
        }
    }

    func sendAudioChunk(_ chunk: Data) {
        writeQueue.async { [weak self] in
            guard let self else { return }
            do {
                guard let sessionID = self.stateQueue.sync(execute: { self.sessionID }) else {
                    throw AppError.missingSessionID
                }
                try self.sendCommandAsync(
                    type: "homebuddy_smart_glasses_service/audio_chunk",
                    payload: [
                        "session_id": sessionID,
                        "audio": chunk.base64EncodedString(),
                        "rate": Int(self.cfg.rate),
                        "width": self.cfg.width,
                        "channels": Int(self.cfg.channels)
                    ]
                )
            } catch {
                self.fail(error)
            }
        }
    }

    func finish() {
        writeQueue.async { [weak self] in
            guard let self else { return }
            do {
                guard let sessionID = self.stateQueue.sync(execute: { self.sessionID }) else {
                    return
                }
                try self.sendCommandAsync(
                    type: "homebuddy_smart_glasses_service/close_stream",
                    payload: ["session_id": sessionID]
                )
            } catch {
                self.fail(error)
            }
        }
    }

    func cancel() {
        close()
    }

    private func authenticate() throws {
        let first = try receiveMessageSync()
        guard (first["type"] as? String) == "auth_required" else {
            throw AppError.authRequired
        }
        try sendRawSync(["type": "auth", "access_token": haToken])
        let second = try receiveMessageSync()
        let eventType = second["type"] as? String ?? ""
        if eventType != "auth_ok" {
            throw AppError.authenticationFailed
        }
    }

    private func sendCommandSync(type: String, payload: [String: Any]) throws -> [String: Any] {
        let id = stateQueue.sync { () -> Int in
            let id = nextMessageID
            nextMessageID += 1
            return id
        }
        let pending = PendingResult()
        stateQueue.sync {
            pendingResults[id] = pending
        }
        var message = payload
        message["id"] = id
        message["type"] = type
        try sendRawSync(message)
        pending.semaphore.wait()
        _ = stateQueue.sync {
            pendingResults.removeValue(forKey: id)
        }
        if let error = pending.error {
            throw error
        }
        return pending.payload ?? [:]
    }

    private func sendCommandAsync(type: String, payload: [String: Any]) throws {
        let id = stateQueue.sync { () -> Int in
            let id = nextMessageID
            nextMessageID += 1
            return id
        }
        var message = payload
        message["id"] = id
        message["type"] = type
        try sendRawSync(message)
    }

    private func sendRawSync(_ object: [String: Any]) throws {
        let isClosed = stateQueue.sync { closed }
        guard !isClosed, let task else { return }
        let raw = try JSONSerialization.data(withJSONObject: object)
        guard let text = String(data: raw, encoding: .utf8) else {
            throw AppError.invalidJSON
        }
        final class SendState: @unchecked Sendable { var error: Error? }
        let sendState = SendState()
        let semaphore = DispatchSemaphore(value: 0)
        task.send(.string(text)) { error in
            sendState.error = error
            semaphore.signal()
        }
        semaphore.wait()
        if let sendError = sendState.error {
            throw sendError
        }
    }

    private func receiveMessageSync() throws -> [String: Any] {
        guard let task else {
            throw AppError.streamConnectionFailed
        }
        final class ReceiveState: @unchecked Sendable {
            var message: URLSessionWebSocketTask.Message?
            var error: Error?
        }
        let receiveState = ReceiveState()
        let semaphore = DispatchSemaphore(value: 0)
        task.receive { result in
            switch result {
            case let .failure(error):
                receiveState.error = error
            case let .success(message):
                receiveState.message = message
            }
            semaphore.signal()
        }
        semaphore.wait()
        if let error = receiveState.error {
            throw error
        }
        guard let message = receiveState.message else {
            throw AppError.streamConnectionFailed
        }
        return try decodeWebSocketMessage(message)
    }

    private func readLoop() {
        let isClosed = stateQueue.sync { closed }
        guard !isClosed, let task else { return }

        task.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case let .failure(error):
                self.fail(error)
            case let .success(message):
                do {
                    let event = try self.decodeWebSocketMessage(message)
                    try self.handleServerEvent(event)
                    self.readLoop()
                } catch {
                    self.fail(error)
                }
            }
        }
    }

    private func fail(_ error: Error) {
        let wasClosed = stateQueue.sync { () -> Bool in
            let value = closed
            closed = true
            return value
        }
        if !wasClosed {
            closeStreams()
            onError?(error)
        }
    }

    private func close() {
        let shouldClose = stateQueue.sync { () -> Bool in
            if closed {
                return false
            }
            closed = true
            return true
        }
        if shouldClose {
            closeStreams()
        }
    }

    private func closeStreams() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
    }

    private func decodeWebSocketMessage(_ message: URLSessionWebSocketTask.Message) throws -> [String: Any] {
        switch message {
        case let .string(text):
            guard let data = text.data(using: .utf8),
                  let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw AppError.invalidJSON
            }
            return decoded
        case let .data(data):
            guard let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw AppError.invalidJSON
            }
            return decoded
        @unknown default:
            throw AppError.invalidJSON
        }
    }

    private func handleServerEvent(_ event: [String: Any]) throws {
        let messageType = event["type"] as? String ?? ""
        if messageType == "result" {
            let id = event["id"] as? Int ?? intValue(event["id"])
            let success = (event["success"] as? Bool) ?? false
            let result = event["result"] as? [String: Any] ?? [:]
            let errorBody = event["error"] as? [String: Any] ?? [:]
            let pending = stateQueue.sync { pendingResults[id] }
            if let pending {
                if success {
                    pending.payload = result
                } else {
                    let message = (errorBody["message"] as? String) ?? "Home Assistant websocket command failed"
                    pending.error = NSError(domain: "HomeBuddySmartGlassesStreamer", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
                }
                pending.semaphore.signal()
            }
            return
        }

        guard messageType == "event" else {
            return
        }

        let data = event["event"] as? [String: Any] ?? [:]
        let type = data["type"] as? String ?? ""
        let text = (data["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let transcript = (data["transcript"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let response = (data["response"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        if type == "transcript_chunk", !text.isEmpty {
            onPartialTranscript?(text)
        } else if type == "transcript" {
            onFinalTranscript?(text)
        } else if type == "agent_response" {
            onAgentResponse?(transcript, response)
        } else if type == "backend" {
            let mode = (data["mode"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !mode.isEmpty {
                onBackendMode?(mode)
            }
        } else if type == "error" {
            let message = (data["message"] as? String ?? "Unknown HomeBuddy error").trimmingCharacters(in: .whitespacesAndNewlines)
            throw NSError(domain: "HomeBuddySmartGlassesStreamer", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
        }
    }
}

func requestMicrophonePermission() -> Bool {
    if AVCaptureDevice.authorizationStatus(for: .audio) == .authorized {
        return true
    }

    let semaphore = DispatchSemaphore(value: 0)
    final class PermissionState: @unchecked Sendable {
        var granted = false
    }
    let permissionState = PermissionState()
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        permissionState.granted = ok
        semaphore.signal()
    }
    semaphore.wait()
    return permissionState.granted
}

func printUsageAndExit() -> Never {
    print("Usage: homebuddy-smart-glasses-streamer [--scheme ws|wss] [--host HOST] [--port PORT] [--codec pcm16|opus] [--mode transcription|agent] --ha-token TOKEN [--language LANG]")
    print("  Default target: ws://homeassistant.local:8123/api/websocket")
    exit(0)
}

@main
struct HomeBuddySmartGlassesStreamer {
    static func main() {
        do {
            let options = try CLIOptions.parse(from: Array(CommandLine.arguments.dropFirst()))

            guard requestMicrophonePermission() else {
                throw AppError.microphoneAccessDenied
            }

            let cfg = AudioConfig.make(codec: options.codec)
            let recorder = try PushToTalkRecorder(cfg: cfg)
            let keyboard = KeyboardMonitor()
            let stateQueue = DispatchQueue(label: "homebuddy.smartglasses.app.state")
            let appState = AppState()

            print("Live streaming ready.")
            print("Streaming microphone audio continuously. Press ESC to quit.")
            print("Target: \(options.scheme)://\(options.host):\(options.port)")
            print("Audio codec: \(options.codec)")
            print("Mode: \(options.mode)")

            let streamingClient = HomeAssistantWebSocketClient(
                scheme: options.scheme,
                host: options.host,
                port: options.port,
                codec: options.codec,
                haToken: options.haToken ?? "",
                language: options.language,
                mode: options.mode,
                cfg: cfg
            )

            streamingClient.onPartialTranscript = { text in
                print("[partial] \(text)")
            }

            streamingClient.onFinalTranscript = { text in
                print("[stt] \(text.isEmpty ? "(no text)" : text)")
            }

            streamingClient.onAgentResponse = { transcript, response in
                print("[agent:stt] \(transcript.isEmpty ? "(no text)" : transcript)")
                print("[agent:reply] \(response.isEmpty ? "(empty response)" : response)")
            }

            streamingClient.onBackendMode = { mode in
                print("[backend] \(mode)")
            }

            streamingClient.onError = { error in
                print("[err] \(error)")
                stateQueue.async {
                    appState.client = nil
                }
            }

            try streamingClient.start()
            stateQueue.sync {
                appState.client = streamingClient
            }
            print("[rec] LIVE")
            recorder.start { chunk in
                streamingClient.sendAudioChunk(chunk)
            }

            keyboard.onEscDown = {
                stateQueue.sync {
                    let durationMs = recorder.stop()
                    print("\n[rec] STOP  (\(durationMs) ms)")
                    appState.client?.finish()
                    appState.client?.cancel()
                    appState.client = nil
                }
                print("\nBye.")
                keyboard.stop()
                CFRunLoopStop(CFRunLoopGetMain())
            }

            try keyboard.start()
            CFRunLoopRun()
        } catch {
            fputs("[fatal] \(error)\n", stderr)
            exit(1)
        }
    }
}
