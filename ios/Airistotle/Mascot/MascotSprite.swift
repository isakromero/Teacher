import SwiftUI

/// Aristotle, the mascot: the app icon's character (`static/img/favicon.svg`, the same face,
/// beard and laurel pixel for pixel) as a small full figure in a white himation, drawn as pixel
/// art. 32 × 32 pixels per frame, on a transparent ground.
///
/// Each frame is a list of rows; each character names a palette entry (`.` is transparent).
/// The figure has a dark outline (`O`), so the white robe and beard still read on the light
/// page background (#f7f5f0) as well as on the dark one (#141312).
enum MascotPose: String, CaseIterable, Sendable {
    /// Standing, arms at the sides.
    case idle
    /// `idle` with the eyes closed.
    case blink
    /// Waving: the hand up, then tilted.
    case wave1, wave2
    /// Both arms up, with a few sparkles.
    case cheer
    /// `cheer` two pixels off the ground (a small hop), the sparkles brighter.
    case hop
}

enum MascotSprite {
    static let width = 32
    static let height = 32

    /// The icon's palette (its blue ground, #28598a, is not used: the figure stands on the page),
    /// with an outline, a robe shade, sandals and sparkles added; as 0xRRGGBB.
    static let colors: [Character: UInt32] = [
        "O": 0x4B4038, // outline
        "G": 0x7FB04A, // laurel
        "H": 0xD8D2C4, // hair
        "S": 0xE6B088, // skin
        "N": 0xC48A60, // skin shade: nose, closed eyes
        "E": 0x2A2019, // eyes, mouth
        "B": 0xF3EFE6, // beard
        "b": 0xC2BBAB, // beard shade, arm lines
        "R": 0xF7F3EA, // robe
        "q": 0xDDD6C7, // robe shade (the drape)
        "r": 0xB9C3D1, // robe folds
        "F": 0x8A5A3B, // sandals
        "K": 0xE3A92B, // sparkle
        "k": 0xF2C94C  // sparkle, light
    ]
    /// The soft ground shadow: black at this opacity.
    static let shadowKey: Character = "w"
    static let shadowOpacity = 0.19

    static let palette: [Character: Color] = {
        var palette = colors.mapValues { value in
            Color(.sRGB, red: Double((value >> 16) & 0xFF) / 255, green: Double((value >> 8) & 0xFF) / 255,
                  blue: Double(value & 0xFF) / 255, opacity: 1)
        }
        palette[shadowKey] = Color(.sRGB, red: 0, green: 0, blue: 0, opacity: shadowOpacity)
        return palette
    }()

    /// A run of same-coloured pixels on one row, drawn as one rectangle.
    struct Run: Equatable, Sendable {
        var row: Int
        var column: Int
        var length: Int
        var key: Character
    }

    /// The frame as runs (transparent pixels left out). The frames are checked by the tests:
    /// every row is `width` long, there are `height` rows, every character is in the palette.
    static func runs(_ pose: MascotPose) -> [Run] { allRuns[pose] ?? [] }

    private static let allRuns = Dictionary(uniqueKeysWithValues: MascotPose.allCases.map { ($0, makeRuns($0)) })

    private static func makeRuns(_ pose: MascotPose) -> [Run] {
        var runs: [Run] = []
        for (row, line) in rows(pose).enumerated() {
            var column = 0
            for (key, group) in line.chunked() {
                if key != "." { runs.append(Run(row: row, column: column, length: group, key: key)) }
                column += group
            }
        }
        return runs
    }

    // swiftlint:disable function_body_length
    static func rows(_ pose: MascotPose) -> [String] {
        switch pose {
        case .idle:
            [
                "................................",
                "................................",
                "................................",
                "................................",
                ".............OOOOOO.............",
                "............OGGGGGGO............",
                "...........OGHHHHHHGO...........",
                "..........OGHHHHHHHHGO..........",
                "..........OHHSSSSSSHHO..........",
                "..........OHSSSSSSSSHO..........",
                "..........OHSESSSSESHO..........",
                "..........OHSSSSNSSSHO..........",
                "..........OHBSSSNNSBHO..........",
                "..........OBBBBNNBBBBO..........",
                "..........OBBBBEEBBBBO..........",
                "..........OObBBBBBBbOO..........",
                ".........ORRRbBBBBbRRRO.........",
                "........ORRRrRbBBbRrRRRO........",
                ".......ORRRrRRRbbRRRrRqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......OSSORRRRRRRRRROSSO.......",
                ".......ONNORRRRRRRRRRONNO.......",
                "........OORRRRRRRRRRRqrO........",
                ".........ORRRRRRRRRRqrRO........",
                "........ORRRRRRRRRRRqrRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "..........wwwwwwwwwwww..........",
                "................................"
            ]
        case .blink:
            [
                "................................",
                "................................",
                "................................",
                "................................",
                ".............OOOOOO.............",
                "............OGGGGGGO............",
                "...........OGHHHHHHGO...........",
                "..........OGHHHHHHHHGO..........",
                "..........OHHSSSSSSHHO..........",
                "..........OHSSSSSSSSHO..........",
                "..........OHSNSSSSNSHO..........",
                "..........OHSSSSNSSSHO..........",
                "..........OHBSSSNNSBHO..........",
                "..........OBBBBNNBBBBO..........",
                "..........OBBBBEEBBBBO..........",
                "..........OObBBBBBBbOO..........",
                ".........ORRRbBBBBbRRRO.........",
                "........ORRRrRbBBbRrRRRO........",
                ".......ORRRrRRRbbRRRrRqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......ORRbRRRRRRRRRRbqRO.......",
                ".......OSSORRRRRRRRRROSSO.......",
                ".......ONNORRRRRRRRRRONNO.......",
                "........OORRRRRRRRRRRqrO........",
                ".........ORRRRRRRRRRqrRO........",
                "........ORRRRRRRRRRRqrRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "..........wwwwwwwwwwww..........",
                "................................"
            ]
        case .wave1:
            [
                "................................",
                "................................",
                "................................",
                "................................",
                ".............OOOOOO.............",
                "............OGGGGGGO............",
                "...........OGHHHHHHGO...........",
                "..........OGHHHHHHHHGO..........",
                "..........OHHSSSSSSHHO..........",
                "....OO....OHSSSSSSSSHO..........",
                "...OSSO...OHSESSSSESHO..........",
                "...OSSO...OHSSSSNSSSHO..........",
                "...ORRbO..OHBSSSNNSBHO..........",
                "...ORRRbO.OBBBBNNBBBBO..........",
                "....ORRRbOOBBBBEEBBBBO..........",
                ".....ORRRbOObBBBBBBbOO..........",
                "......ORRRRRRbBBBBbRRRO.........",
                ".......ORRRRrRbBBbRrRRRO........",
                "........OORrRRRbbRRRrRqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRROSSO.......",
                ".........ORRRRRRRRRRRONNO.......",
                ".........ORRRRRRRRRRRqrO........",
                ".........ORRRRRRRRRRqrRO........",
                "........ORRRRRRRRRRRqrRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "..........wwwwwwwwwwww..........",
                "................................"
            ]
        case .wave2:
            [
                "................................",
                "................................",
                "................................",
                "................................",
                ".............OOOOOO.............",
                "............OGGGGGGO............",
                "...........OGHHHHHHGO...........",
                "..........OGHHHHHHHHGO..........",
                "..........OHHSSSSSSHHO..........",
                ".....OO...OHSSSSSSSSHO..........",
                "....OSSO..OHSESSSSESHO..........",
                "...OSSO...OHSSSSNSSSHO..........",
                "...ORRbO..OHBSSSNNSBHO..........",
                "...ORRRbO.OBBBBNNBBBBO..........",
                "....ORRRbOOBBBBEEBBBBO..........",
                ".....ORRRbOObBBBBBBbOO..........",
                "......ORRRRRRbBBBBbRRRO.........",
                ".......ORRRRrRbBBbRrRRRO........",
                "........OORrRRRbbRRRrRqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRRbqRO.......",
                ".........ORRRRRRRRRRROSSO.......",
                ".........ORRRRRRRRRRRONNO.......",
                ".........ORRRRRRRRRRRqrO........",
                ".........ORRRRRRRRRRqrRO........",
                "........ORRRRRRRRRRRqrRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "..........wwwwwwwwwwww..........",
                "................................"
            ]
        case .cheer:
            [
                "................................",
                "................................",
                "......k.........................",
                "..........................k.....",
                ".............OOOOOO.............",
                "............OGGGGGGO............",
                "..k........OGHHHHHHGO...........",
                "..........OGHHHHHHHHGO..........",
                ".....OO...OHHSSSSSSHHO...OO..k..",
                "....OSSO..OHSSSSSSSSHO..OSSO....",
                "....OSSO..OHSESSSSESHO..OSSO....",
                "....ORRbO.OHSSSSNSSSHO.ObRRO....",
                "....ORRRbOOHBSSSNNSBHOObRRRO....",
                ".....ORRROOBBBBNNBBBBOORRRO.....",
                ".....ORRRbOBBBBEEBBBBObRRRO.....",
                "......ORRROObBBBBBBbOORRRO......",
                "......ORRRRRRbBBBBbRRRRRRO......",
                ".......ORRRRrRbBBbRrRRRRO.......",
                "........OORrRRRbbRRRrROO........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRqrRO.........",
                "........ORRRRRRRRRRqrRRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "..........wwwwwwwwwwww..........",
                "................................"
            ]
        case .hop:
            [
                "................................",
                ".......k................k.......",
                "..k..........OOOOOO.............",
                ".kKk........OGGGGGGO............",
                "..k........OGHHHHHHGO.......k...",
                "..........OGHHHHHHHHGO.....kKk..",
                ".....OO...OHHSSSSSSHHO...OO.k...",
                "....OSSO..OHSSSSSSSSHO..OSSO....",
                "....OSSO..OHSESSSSESHO..OSSO....",
                "....ORRbO.OHSSSSNSSSHO.ObRRO....",
                "....ORRRbOOHBSSSNNSBHOObRRRO....",
                ".....ORRROOBBBBNNBBBBOORRRO..k..",
                ".....ORRRbOBBBBEEBBBBObRRRO.....",
                "......ORRROObBBBBBBbOORRRO......",
                "......ORRRRRRbBBBBbRRRRRRO......",
                ".......ORRRRrRbBBbRrRRRRO.......",
                "........OORrRRRbbRRRrROO........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqRO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRRqrO.........",
                ".........ORRRRRRRRRqrRO.........",
                "........ORRRRRRRRRRqrRRO........",
                "........ORrRRRrRRRRrRRrO........",
                ".........OOFFFOOOOFFFOO.........",
                "...........OOO....OOO...........",
                "................................",
                "............wwwwwwww............",
                "................................"
            ]
        }
    }
    // swiftlint:enable function_body_length
}

private extension String {
    /// Runs of equal characters: ("a", 2), ("b", 1) for "aab".
    func chunked() -> [(Character, Int)] {
        var out: [(Character, Int)] = []
        for character in self {
            if let last = out.last, last.0 == character {
                out[out.count - 1].1 += 1
            } else {
                out.append((character, 1))
            }
        }
        return out
    }
}

/// One frame of the mascot, `pixel` points to a sprite pixel (a whole number, so every pixel
/// edge falls on a device pixel: no smoothing between pixels).
struct MascotSpriteView: View {
    let pose: MascotPose
    var pixel: CGFloat = 2

    var body: some View {
        let size = pixel.rounded()
        Canvas { context, _ in
            for run in MascotSprite.runs(pose) {
                guard let color = MascotSprite.palette[run.key] else {
                    assertionFailure("no palette entry for \(run.key)")
                    continue
                }
                let rect = CGRect(x: CGFloat(run.column) * size, y: CGFloat(run.row) * size,
                                  width: CGFloat(run.length) * size, height: size)
                context.fill(Path(rect), with: .color(color), style: FillStyle(antialiased: false))
            }
        }
        .frame(width: CGFloat(MascotSprite.width) * size, height: CGFloat(MascotSprite.height) * size)
        .accessibilityHidden(true)
    }
}
