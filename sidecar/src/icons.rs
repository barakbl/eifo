//! The dot in the menu bar.
//!
//! Drawn rather than shipped as assets: it is a small round LED in one of four
//! colours, and a PNG per colour per scale would be four files to keep in step
//! with a palette that lives in one line of code.
//!
//! Not a flat disc. It is shaded like a lit bead - brighter towards the centre,
//! a darker rim for a defined edge, and a soft specular highlight up and to the
//! left - so it reads as a status light rather than a printed circle.
//!
//! Not a template image. macOS template icons are recoloured by the system to
//! match the menu bar, which is the right choice for a glyph that means
//! something by its shape - and exactly the wrong one here, where the colour
//! *is* the message.

use tray_icon::Icon;

use crate::health::Status;

/// Rendered at 2x and given a 1x logical size, so the circle stays smooth on a
/// Retina display instead of being scaled up from 18 points of pixels.
const SIZE: u32 = 36;

/// Apple's system colours, so the dot looks like it belongs to the OS rather
/// than to whoever picked a green.
const GREEN: [u8; 3] = [0x30, 0xD1, 0x58];
const ORANGE: [u8; 3] = [0xFF, 0x9F, 0x0A];
const RED: [u8; 3] = [0xFF, 0x45, 0x3A];
const GREY: [u8; 3] = [0x8E, 0x8E, 0x93];

pub fn colour_of(status: Status) -> [u8; 3] {
    match status {
        Status::Ok => GREEN,
        Status::Attention => ORANGE,
        Status::Down => RED,
        Status::Unknown => GREY,
    }
}

pub fn icon_for(status: Status) -> Icon {
    let rgba = led(colour_of(status));
    Icon::from_rgba(rgba, SIZE, SIZE).expect("the buffer is SIZE*SIZE*4 by construction")
}

/// A round LED: an antialiased disc, shaded like a lit bead.
///
/// Coverage is sampled from the distance to the centre rather than by
/// supersampling: one circle needs one smooth edge, and a hard-edged dot at
/// this size looks like a rendering bug rather than a design. On top of that
/// edge the fill is not flat - a gentle spherical shade brightens the centre
/// and darkens the rim, and a soft off-centre highlight gives it the wet look
/// of a real indicator light.
fn led(colour: [u8; 3]) -> Vec<u8> {
    let size = SIZE as f32;
    // A whisker of inset, so the antialiased edge has somewhere to land instead
    // of being clipped flat by the edge of the bitmap.
    let radius = size / 2.0 - 1.5;
    let centre = size / 2.0 - 0.5;
    // The specular highlight sits up and to the left, where a light above the
    // screen would put it.
    let hx = centre - radius * 0.35;
    let hy = centre - radius * 0.40;

    let mut rgba = Vec::with_capacity((SIZE * SIZE * 4) as usize);
    for y in 0..SIZE {
        for x in 0..SIZE {
            let dx = x as f32 - centre;
            let dy = y as f32 - centre;
            let distance = (dx * dx + dy * dy).sqrt();
            // One pixel of feathering either side of the edge.
            let coverage = ((radius + 0.5 - distance).clamp(0.0, 1.0) * 255.0).round() as u8;
            if coverage == 0 {
                rgba.extend_from_slice(&[0, 0, 0, 0]);
                continue;
            }

            let t = (distance / radius).clamp(0.0, 1.0);
            // Spherical shade: a touch brighter than base at the centre, falling
            // away towards the edge.
            let mut shade = 1.12 - 0.45 * t * t;
            // A darker rim in the last fifth of the radius, for a defined edge.
            if t > 0.80 {
                shade *= 1.0 - 0.30 * ((t - 0.80) / 0.20).clamp(0.0, 1.0);
            }
            let mut r = colour[0] as f32 * shade;
            let mut g = colour[1] as f32 * shade;
            let mut b = colour[2] as f32 * shade;

            // Specular highlight: pull the fill towards white near the hot spot.
            let hd = (((x as f32 - hx).powi(2) + (y as f32 - hy).powi(2)).sqrt()) / (radius * 0.95);
            let spec = (1.0 - hd).clamp(0.0, 1.0).powf(2.2) * 0.55;
            r += (255.0 - r) * spec;
            g += (255.0 - g) * spec;
            b += (255.0 - b) * spec;

            rgba.extend_from_slice(&[
                r.round().clamp(0.0, 255.0) as u8,
                g.round().clamp(0.0, 255.0) as u8,
                b.round().clamp(0.0, 255.0) as u8,
                coverage,
            ]);
        }
    }
    rgba
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_buffer_is_the_size_the_icon_expects() {
        assert_eq!(led(GREEN).len(), (SIZE * SIZE * 4) as usize);
    }

    #[test]
    fn the_centre_is_opaque_and_green_and_the_corners_are_not() {
        let rgba = led(GREEN);
        let at = |x: u32, y: u32| {
            let index = ((y * SIZE + x) * 4) as usize;
            (
                rgba[index],
                rgba[index + 1],
                rgba[index + 2],
                rgba[index + 3],
            )
        };
        let (r, g, b, a) = at(SIZE / 2, SIZE / 2);
        assert_eq!(a, 255, "the middle of the LED must be solid");
        assert!(g > r && g > b, "the green LED must still read as green");
        assert_eq!(at(0, 0).3, 0, "a circle must not fill its corners");
        assert_eq!(at(SIZE - 1, SIZE - 1).3, 0);
    }

    #[test]
    fn it_is_shaded_rather_than_flat() {
        // A real indicator light is not one colour edge to edge: the highlit
        // side is brighter than the far side.
        let rgba = led(GREEN);
        let green_at = |x: u32, y: u32| rgba[(((y * SIZE + x) * 4) + 1) as usize];
        assert!(
            green_at(SIZE / 3, SIZE / 3) > green_at(2 * SIZE / 3, 2 * SIZE / 3),
            "the highlight side should be brighter than the opposite side"
        );
    }

    #[test]
    fn the_edge_is_feathered_rather_than_stepped() {
        let rgba = led(GREEN);
        let alphas: Vec<u8> = (0..SIZE)
            .map(|x| rgba[(((SIZE / 2) * SIZE + x) * 4 + 3) as usize])
            .collect();
        assert!(
            alphas.iter().any(|a| *a > 0 && *a < 255),
            "no partially covered pixel: the edge is hard, which reads as a bug"
        );
    }

    #[test]
    fn every_status_has_its_own_colour() {
        let all = [Status::Ok, Status::Attention, Status::Down, Status::Unknown];
        let colours: Vec<[u8; 3]> = all.iter().map(|s| colour_of(*s)).collect();
        for (index, colour) in colours.iter().enumerate() {
            assert!(
                !colours[index + 1..].contains(colour),
                "two states share a colour, so the dot cannot tell them apart"
            );
        }
    }
}
