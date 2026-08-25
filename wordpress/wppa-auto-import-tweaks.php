<?php
/**
 * Plugin Name: WPPA Auto Import display tweaks
 * Description: Update-safe frontend support for wppa-auto-import descriptions, custom EXIF labels and thumbnail captions.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

function wppa_auto_import_mobile_options() {
	if ( function_exists( 'wppa_initialize_runtime' ) && function_exists( 'wppa_is_mobile' ) && wppa_is_mobile() ) {
		wppa_initialize_runtime();
		global $wppa_opt;
		$wppa_opt['wppa_thumbsize'] = 240;
		$wppa_opt['wppa_thumbsize_alt'] = 240;
	}
}
add_action( 'init', 'wppa_auto_import_mobile_options', 999 );
add_action( 'rest_api_init', 'wppa_auto_import_mobile_options', 999 );
add_action( 'wp', 'wppa_auto_import_mobile_options', 1 );

function wppa_auto_import_refresh_random_covers() {
	if ( ! function_exists( 'wppa_expand_enum' ) || ! function_exists( 'wppa_alb_to_enum_children' ) ) {
		return;
	}
	if ( wppa_opt( 'main_photo' ) != '-3' ) return;
	global $wpdb;
	$refreshed_albums = [];
	$albums = $wpdb->get_results( "SELECT id,main_photo FROM {$wpdb->wppa_albums} WHERE main_photo IN (0,-3)", ARRAY_A );
	foreach ( $albums as $album ) {
		$album_id = (int) $album['id'];
		$album_ids = wppa_expand_enum( wppa_alb_to_enum_children( $album_id ) );
		$album_ids = array_filter( array_map( 'intval', explode( ',', str_replace( '.', ',', $album_ids ) ) ) );
		if ( empty( $album_ids ) ) $album_ids = [ (int) $album_id ];
		$placeholders = implode( ',', array_fill( 0, count( $album_ids ), '%d' ) );
		$random_photo = $wpdb->get_var( $wpdb->prepare(
			"SELECT id FROM {$wpdb->wppa_photos} WHERE album IN ($placeholders) AND status = 'publish' AND ext <> 'xxx' ORDER BY RAND() LIMIT 1",
			$album_ids
		) );
		if ( $random_photo ) {
			$wpdb->update( $wpdb->wppa_albums, [ 'main_photo' => (int) $random_photo ], [ 'id' => (int) $album_id ], [ '%d' ], [ '%d' ] );
			$refreshed_albums[] = [ $album_id, (int) $album['main_photo'] ];
		}
	}
	register_shutdown_function( function () use ( $wpdb, $refreshed_albums ) {
		foreach ( $refreshed_albums as [ $album_id, $main_photo ] ) {
			$wpdb->update( $wpdb->wppa_albums, [ 'main_photo' => $main_photo ], [ 'id' => $album_id ], [ '%d' ], [ '%d' ] );
		}
	} );
}
add_action( 'wp', 'wppa_auto_import_refresh_random_covers', 99 );
add_action( 'rest_api_init', 'wppa_auto_import_refresh_random_covers', 1000 );

function wppa_auto_import_filter_output( $html ) {
	global $wpdb;
	static $custom_exif_labels = null;

	$html = str_replace( '@@BR@@', '<br>', $html );

	if ( null === $custom_exif_labels && isset( $wpdb->wppa_exif ) ) {
		$custom_exif_labels = $wpdb->get_results(
			"SELECT tag, description FROM {$wpdb->wppa_exif} WHERE photo = 0 AND tag LIKE 'X#%'",
			OBJECT_K
		);
	}
	if ( is_array( $custom_exif_labels ) ) {
		foreach ( $custom_exif_labels as $tag => $row ) {
			$hex   = strtoupper( substr( (string) $tag, 2, 4 ) );
			$label = rtrim( (string) $row->description, " \n\r\t\v\0:" );
			$html  = str_replace( 'UndefinedTag:0x' . $hex, esc_html( $label ), $html );
		}
	}

	return $html;
}

add_action( 'init', function () {
	if ( ! is_admin() || wp_doing_ajax() ) {
		ob_start( 'wppa_auto_import_filter_output' );
	}
}, PHP_INT_MIN );

add_action( 'wp_head', function () {
	?>
	<style id="wppa-auto-import-styles">
		.wppa-thumb-area {
			border: none !important;
			overflow: hidden !important;
		}
		.wppa-calendar > div { overflow: hidden !important; }
		.wppa-caption-host { position: relative; overflow: hidden; }
		.wppa-caption-host > .wppa-thumb-text {
			position: absolute !important;
			left: 0;
			right: 0;
			bottom: 0;
			top: auto !important;
			background: rgba(0, 0, 0, 0.75) !important;
			color: #fff !important;
			padding: 4px 6px !important;
			font-size: 11px !important;
			line-height: 1.25 !important;
			margin: 0 !important;
			transform: translateY(100%) !important;
			opacity: 0 !important;
			transition: transform .2s ease, opacity .2s ease !important;
			pointer-events: none !important;
			z-index: 5 !important;
			max-height: 100% !important;
			overflow: hidden !important;
			box-sizing: border-box !important;
		}
		.wppa-caption-host.wppa-caption-visible > .wppa-thumb-text {
			transform: translateY(0) !important;
			opacity: 1 !important;
		}
	</style>
	<?php
} );

add_action( 'wp_footer', function () {
	?>
	<script>
	(function () {
		function makeCaptionHost( host ) {
			if ( host.classList.contains( 'wppa-caption-host' ) ) return;
			host.classList.add( 'wppa-caption-host' );
			host.style.position = 'relative';
			host.addEventListener( 'mouseenter', function () { host.classList.add( 'wppa-caption-visible' ); } );
			host.addEventListener( 'mouseleave', function () { host.classList.remove( 'wppa-caption-visible' ); } );
			host.addEventListener( 'touchstart', function () { host.classList.toggle( 'wppa-caption-visible' ); }, { passive: true } );
		}

		function initCaptionOverlays() {
			document.querySelectorAll( '.wppa-thumb-text' ).forEach( function ( caption ) {
				if ( caption.innerHTML.indexOf( '@@BR@@' ) !== -1 ) {
					caption.innerHTML = caption.innerHTML.replace( /@@BR@@/g, '<br>' );
				}
				if ( ! caption.textContent.trim() ) return;
				if ( caption.parentElement ) makeCaptionHost( caption.parentElement );
			} );

			document.querySelectorAll( 'img[id^="i-"][title], video[id^="i-"][title]' ).forEach( function ( media ) {
				var text = media.getAttribute( 'title' );
				if ( ! text ) return;
				media.removeAttribute( 'title' );
				var host = media.closest( '[id^="thumbnail_frame_masonry_"], [id^="thumbphoto_frame_"], [id^="thumbnail_frame_"]' ) || media.parentElement;
				if ( ! host || host.querySelector( ':scope > .wppa-thumb-text' ) ) return;
				var caption = document.createElement( 'div' );
				caption.className = 'wppa-thumb-text';
				caption.innerHTML = text.split( /@@BR@@|<br\s*\/?\s*>|\n/i ).filter( Boolean ).join( '<br>' );
				var padding = parseFloat( getComputedStyle( media ).paddingLeft ) || 0;
				if ( padding ) {
					caption.style.left = padding + 'px';
					caption.style.right = padding + 'px';
					caption.style.bottom = padding + 'px';
				}
				host.appendChild( caption );
				makeCaptionHost( host );
			} );
		}

		document.addEventListener( 'DOMContentLoaded', initCaptionOverlays );
		window.addEventListener( 'pageshow', initCaptionOverlays );
		document.addEventListener( 'visibilitychange', function () {
			if ( ! document.hidden ) initCaptionOverlays();
		} );
		var captionTimer = 0;
		var captionObserver = new MutationObserver( function ( mutations ) {
			var hasWppaContent = mutations.some( function ( mutation ) {
				return Array.prototype.some.call( mutation.addedNodes, function ( node ) {
					return node.nodeType === 1 && (
						node.matches( '.wppa-container, .wppa-thumb-area, [id^="thumbnail_frame_"]' ) ||
						node.querySelector( '.wppa-container, .wppa-thumb-area, [id^="thumbnail_frame_"]' )
					);
				} );
			} );
			if ( ! hasWppaContent ) return;
			window.clearTimeout( captionTimer );
			captionTimer = window.setTimeout( function () {
				captionObserver.disconnect();
				initCaptionOverlays();
				captionObserver.takeRecords();
				captionObserver.observe( document.body, { childList: true, subtree: true } );
			}, 50 );
		} );
		if ( document.body ) {
			captionObserver.observe( document.body, { childList: true, subtree: true } );
		}
	}());
	</script>
	<?php
} );
