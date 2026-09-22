import AppKit
let destination = CommandLine.arguments[1]
let image = NSImage(size: NSSize(width: 1024, height: 1024), flipped: false) { _ in
    NSColor(calibratedRed: 0.043, green: 0.071, blue: 0.125, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: 1024, height: 1024), xRadius: 230, yRadius: 230).fill()
    let mint = NSColor(calibratedRed: 0.47, green: 0.93, blue: 0.76, alpha: 1)
    let cyan = NSColor(calibratedRed: 0.44, green: 0.75, blue: 1, alpha: 1)
    let purple = NSColor(calibratedRed: 0.67, green: 0.61, blue: 1, alpha: 1)
    for (x, y, size, color) in [(90.0, 430.0, 330.0, cyan), (604.0, 430.0, 330.0, purple), (292.0, 230.0, 440.0, mint)] {
        let body = NSBezierPath(roundedRect: NSRect(x: x-12, y: y-size*0.3, width: size+24, height: size*0.6), xRadius: size*0.25, yRadius: size*0.25)
        NSColor(calibratedRed: 0.094, green: 0.17, blue: 0.25, alpha: 1).setFill(); body.fill()
        color.setStroke(); body.lineWidth = 12; body.stroke()
        let hood = NSBezierPath(ovalIn: NSRect(x:x,y:y,width:size,height:size))
        NSColor(calibratedRed: 0.094, green: 0.17, blue: 0.25, alpha: 1).setFill(); hood.fill()
        color.setStroke();hood.lineWidth=12;hood.stroke()
        color.setFill()
        NSBezierPath(roundedRect: NSRect(x:x+size*0.13,y:y+size*0.40,width:size*0.74,height:size*0.22),xRadius:24,yRadius:24).fill()
        NSBezierPath(roundedRect: NSRect(x:x+size*0.12,y:y-size*0.1,width:size*0.76,height:size*0.15),xRadius:12,yRadius:12).fill()
        NSColor(calibratedRed:0.03,green:0.07,blue:0.11,alpha:1).setFill()
        for eye in [0.27,0.61] { NSBezierPath(ovalIn:NSRect(x:x+size*eye,y:y+size*0.47,width:size*0.12,height:size*0.08)).fill() }
    }
    return true
}
let data = NSBitmapImageRep(data:image.tiffRepresentation!)!.representation(using:.png,properties:[:])!
try data.write(to:URL(fileURLWithPath:destination))
