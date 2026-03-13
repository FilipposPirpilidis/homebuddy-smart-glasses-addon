// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "homebuddy-smart-glasses-streamer",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        .executableTarget(
            name: "homebuddy-smart-glasses-streamer"
        )
    ]
)
