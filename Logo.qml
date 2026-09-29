import QtQuick
import QtQuick.Shapes
import qs.Commons

// The File Vault mark: a hexagonal vault with a dial ring and a core.
//
// The phase changes the mark rather than swapping icons:
//   idle     all in the bar's colour
//   waiting  the core in the accent: copied changes wait to be pushed
//   busy     the dial ring turns, in the accent: connecting, copying, pushing
//   error    the core in the urgent colour
//
// Drawn on a 16-unit grid and scaled as vectors, so it stays sharp at the
// bar's icon size.
Item {
  id: logo

  property string phase: "idle"
  property color color: Color.foreground
  property color accent: Color.accent
  property color urgent: Color.urgent

  readonly property color ringColor: phase === "busy" ? accent : color
  readonly property color coreColor: phase === "error" ? urgent
    : phase === "waiting" || phase === "busy" ? accent : color

  implicitWidth: 16
  implicitHeight: 16

  Item {
    id: grid
    width: 16
    height: 16
    scale: Math.min(logo.width, logo.height) / 16
    transformOrigin: Item.TopLeft
    x: (logo.width - 16 * scale) / 2
    y: (logo.height - 16 * scale) / 2

    // The vault: a pointed-top hexagon, open at the upper right like a cut
    // corner.
    Shape {
      anchors.fill: parent
      preferredRendererType: Shape.CurveRenderer

      ShapePath {
        strokeColor: logo.color
        strokeWidth: 1.35
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        PathSvg { path: "M 11.4 2.95 L 14.06 4.5 L 14.06 11.5 L 8 15 L 1.94 11.5 L 1.94 4.5 L 8 1 L 9.3 1.75" }
      }
    }

    // The dial: three quarters of a ring, turning while a job runs.
    Shape {
      id: dial
      width: 16
      height: 16
      preferredRendererType: Shape.CurveRenderer
      transformOrigin: Item.Center

      ShapePath {
        strokeColor: logo.ringColor
        strokeWidth: 1.2
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        PathSvg { path: "M 8 4.9 A 3.1 3.1 0 1 1 4.9 8" }
      }

      RotationAnimator on rotation {
        running: logo.phase === "busy"
        from: 0
        to: 360
        duration: 1200
        loops: Animation.Infinite
        onRunningChanged: if (!running) dial.rotation = 0
      }
    }

    // The core.
    Rectangle {
      x: 8 - width / 2
      y: 8 - height / 2
      width: 2.6
      height: 2.6
      radius: width / 2
      color: logo.coreColor
    }
  }
}
