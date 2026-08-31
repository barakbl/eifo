//! The dot in the menu bar.
//!
//! Drawn rather than shipped as assets: it is a filled circle in one of four
//! colours, and a PNG per colour per scale would be four files to keep in step
//! with a palette that lives in one line of code.
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
    let rgba = circle(colour_of(status));
    Icon::from_rgba(rgba, SIZE, SIZE).expect("the buffer is SIZE*SIZE*4 by construction")
}

/// A filled circle with an antialiased edge.
///
/// Coverage is sampled from the distance to the centre rather than by
/// supersampling: one circle needs one smooth edge, and a hard-edged dot at
/// this size looks like a rendering bug rather than a design.
fn circle(colour: [u8; 3]) -> Vec<u8> {
    let size = SIZE as f32;
    // A whisker of inset, so the antialiased edge has somewhere to land instead
    // of being clipped flat by the edge of the bitmap.
    let radius = size / 2.0 - 1.5;
    let centre = size / 2.0 - 0.5;

    let mut rgba = Vec::with_capacity((SIZE * SIZE * 4) as usize);
    for y in 0..SIZE {
        for x in 0..SIZE {
            let dx = x as f32 - centre;
            let dy = y as f32 - centre;
            let distance = (dx * dx + dy * dy).sqrt();
            // One pixel of feathering either side of the edge.
            let coverage = ((radius + 0.5 - distance).clamp(0.0, 1.0) * 255.0).round() as u8;
            rgba.extend_from_slice(&[colour[0], colour[1], colour[2], coverage]);
        }
    }
    rgba
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_buffer_is_the_size_the_icon_expects() {
        assert_eq!(circle(GREEN).len(), (SIZE * SIZE * 4) as usize);
    }

    #[test]
    fn the_centre_is_opaque_and_the_corners_are_not() {
        let rgba = circle(GREEN);
        let at = |x: u32, y: u32| {
            let index = ((y * SIZE + x) * 4) as usize;
            (
                rgba[index],
                rgba[index + 1],
                rgba[index + 2],
                rgba[index + 3],
            )
        };
        assert_eq!(at(SIZE / 2, SIZE / 2), (GREEN[0], GREEN[1], GREEN[2], 255));
        assert_eq!(at(0, 0).3, 0, "a circle must not fill its corners");
        assert_eq!(at(SIZE - 1, SIZE - 1).3, 0);
    }

    #[test]
    fn the_edge_is_feathered_rather_than_stepped() {
        let rgba = circle(GREEN);
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
