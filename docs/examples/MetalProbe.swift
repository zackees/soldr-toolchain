import AppKit
import Metal
@MainActor
@_cdecl("soldr_metal_appkit_probe")
public func metalAppKitProbe() -> UInt64 {
    let device = MTLCreateSystemDefaultDevice()
    let color = NSColor(deviceRed: 0.25, green: 0.5, blue: 0.75, alpha: 1)
    return color.alphaComponent == 1 ? (device?.registryID ?? 0) : 0
}
