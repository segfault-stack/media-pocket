use futures::{StreamExt, stream};
use librespot::{
    core::{Session, SessionConfig, SpotifyUri, authentication::Credentials, cache::Cache},
    metadata::{
        Album, Metadata, Playlist,
        audio::{AudioItem, UniqueFields},
    },
    playback::{
        audio_backend,
        config::{AudioFormat, Bitrate, PlayerConfig, VolumeCtrl},
        mixer::{self, MixerConfig},
        player::{Player, PlayerEvent},
    },
};
use librespot::protocol::authentication::AuthenticationType;
use log::error;
use oauth2::{
    AuthUrl, AuthorizationCode, ClientId, CsrfToken, EmptyExtraTokenFields, EndpointNotSet,
    EndpointSet, PkceCodeChallenge, RedirectUrl, Scope, StandardTokenResponse, TokenResponse,
    TokenUrl,
    basic::{BasicClient, BasicTokenType},
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    env,
    io::{BufRead, BufReader, Write},
    net::{SocketAddr, TcpListener},
    path::{Path, PathBuf},
    process,
    time::Duration,
};
use url::Url;

const OAUTH_SCOPES: &[&str] = &[
    "app-remote-control",
    "playlist-modify",
    "playlist-modify-private",
    "playlist-modify-public",
    "playlist-read",
    "playlist-read-collaborative",
    "playlist-read-private",
    "streaming",
    "ugc-image-upload",
    "user-follow-modify",
    "user-follow-read",
    "user-library-modify",
    "user-library-read",
    "user-modify",
    "user-modify-playback-state",
    "user-modify-private",
    "user-personalized",
    "user-read-birthdate",
    "user-read-currently-playing",
    "user-read-email",
    "user-read-play-history",
    "user-read-playback-position",
    "user-read-playback-state",
    "user-read-private",
    "user-read-recently-played",
    "user-top-read",
];

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();
    if let Err(err) = run().await {
        error!("{err}");
        eprintln!("spotify-streamer: {err}");
        process::exit(1);
    }
}

async fn run() -> Result<(), String> {
    let args = Args::parse(env::args().skip(1).collect())?;
    match args.command.as_str() {
        "auth" => auth(args).await,
        "resolve" => resolve(args).await,
        "stream" => stream(args).await,
        "credentials-check" => {
            load_credentials().await?;
            eprintln!("Spotify credentials are present in PostgreSQL");
            Ok(())
        }
        "credentials-delete" => delete_credentials().await,
        "--version" | "version" => {
            println!("spotify-streamer 0.1.0 librespot-latest");
            Ok(())
        }
        _ => Err(usage()),
    }
}

async fn auth(args: Args) -> Result<(), String> {
    let cache = create_cache(&args.cache)?;
    let session_config = SessionConfig::default();
    let redirect_uri = redirect_uri(&args.redirect_host, args.oauth_port);
    eprintln!("Spotify OAuth redirect URI: {redirect_uri}");
    let token = get_access_token(&session_config.client_id, &redirect_uri, &args.bind_host)?;
    let credentials = Credentials::with_access_token(token.access_token().secret());
    let session = Session::new(session_config, Some(cache));
    session
        .connect(credentials, false)
        .await
        .map_err(|e| format!("failed to validate Spotify credentials: {e}"))?;
    let reusable_credentials = Credentials {
        username: Some(session.username()),
        auth_type: AuthenticationType::AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS,
        auth_data: session.auth_data(),
    };
    store_credentials(&reusable_credentials).await?;
    eprintln!("Spotify credentials stored in PostgreSQL");
    Ok(())
}

async fn resolve(args: Args) -> Result<(), String> {
    let uri = parse_spotify_uri(
        args.uri
            .as_deref()
            .ok_or_else(|| "--uri is required".to_string())?,
    )?;
    let session = connect_cached(&args.cache).await?;
    let output = match &uri {
        SpotifyUri::Track { .. } => {
            let item = AudioItem::get_file(&session, uri)
                .await
                .map_err(|e| format!("failed to resolve Spotify track: {e}"))?;
            track_json(&item)?
        }
        SpotifyUri::Album { .. } => {
            let album = Album::get(&session, &uri)
                .await
                .map_err(|e| format!("failed to resolve Spotify album: {e}"))?;
            let track_uris = album.tracks().cloned().collect();
            collection_json(&session, "album", &album.name, &uri, track_uris).await?
        }
        SpotifyUri::Playlist { .. } => {
            let playlist = Playlist::get(&session, &uri)
                .await
                .map_err(|e| format!("failed to resolve Spotify playlist: {e}"))?;
            if playlist.contents.is_truncated {
                log::warn!(
                    "Spotify returned truncated contents for playlist {}",
                    uri.to_uri()
                );
            }
            let track_uris = playlist.tracks().cloned().collect();
            collection_json(&session, "playlist", playlist.name(), &uri, track_uris).await?
        }
        _ => return Err("only Spotify tracks, albums, and playlists are supported".to_string()),
    };
    println!("{output}");
    Ok(())
}

async fn collection_json(
    session: &Session,
    item_type: &str,
    name: &str,
    uri: &SpotifyUri,
    track_uris: Vec<SpotifyUri>,
) -> Result<Value, String> {
    let total = track_uris.len();
    let mut resolved = stream::iter(track_uris.into_iter().enumerate())
        .map(|(index, track_uri)| async move {
            if !matches!(track_uri, SpotifyUri::Track { .. }) {
                return (index, None);
            }
            let result = AudioItem::get_file(session, track_uri)
                .await
                .map_err(|e| e.to_string())
                .and_then(|item| track_json(&item));
            if let Err(error) = &result {
                log::warn!("Skipping unavailable Spotify collection entry: {error}");
            }
            (index, result.ok())
        })
        .buffer_unordered(16)
        .collect::<Vec<_>>()
        .await;
    resolved.sort_by_key(|(index, _)| *index);
    let tracks: Vec<Value> = resolved
        .into_iter()
        .filter_map(|(_, track)| track)
        .collect();
    let skipped = total.saturating_sub(tracks.len());
    if tracks.is_empty() {
        return Err(format!("Spotify {item_type} contains no playable tracks"));
    }

    Ok(json!({
        "type": item_type,
        "name": name,
        "uri": uri.to_uri(),
        "tracks": tracks,
        "skipped": skipped,
    }))
}

fn track_json(item: &AudioItem) -> Result<Value, String> {
    let artists = artist_names(item);
    Ok(json!({
        "type": "track",
        "identifier": canonical_track_uri(&item.track_id)?,
        "uri": item.uri,
        "title": item.name,
        "author": artists.join(", "),
        "durationMs": item.duration_ms,
        "isStream": false,
        "artworkUrl": item.covers.first().map(|c| c.url.as_str()),
    }))
}

async fn stream(args: Args) -> Result<(), String> {
    let uri = parse_track_uri(
        args.uri
            .as_deref()
            .ok_or_else(|| "--uri is required".to_string())?,
    )?;
    let session = connect_cached(&args.cache).await?;
    let mut player_config = PlayerConfig {
        bitrate: bitrate(args.bitrate)?,
        normalisation: false,
        gapless: false,
        position_update_interval: Some(Duration::from_secs(1)),
        ..PlayerConfig::default()
    };
    player_config.ditherer = None;

    let mixer_fn =
        mixer::find(Some("softvol")).ok_or_else(|| "softvol mixer is unavailable".to_string())?;
    let mixer = mixer_fn(MixerConfig {
        volume_ctrl: VolumeCtrl::Fixed,
        ..MixerConfig::default()
    })
    .map_err(|e| format!("failed to create mixer: {e}"))?;
    mixer.set_volume(u16::MAX);
    let soft_volume = mixer.get_soft_volume();

    let sink_builder = audio_backend::find(Some("pipe".to_string()))
        .ok_or_else(|| "pipe audio backend is unavailable".to_string())?;
    let player = Player::new(player_config, session, soft_volume, move || {
        sink_builder(None, AudioFormat::S16)
    });
    let mut events = player.get_player_event_channel();
    player.load(uri, true, 0);

    while let Some(event) = events.recv().await {
        match event {
            PlayerEvent::EndOfTrack { .. } | PlayerEvent::Stopped { .. } => return Ok(()),
            PlayerEvent::Unavailable { track_id, .. } => {
                return Err(format!(
                    "Spotify track is unavailable: {}",
                    track_id.to_uri()
                ));
            }
            _ => {}
        }
    }

    Err("Spotify playback event channel closed".to_string())
}

async fn connect_cached(cache_dir: &Path) -> Result<Session, String> {
    let cache = create_cache(cache_dir)?;
    let credentials = load_credentials().await?;
    let session = Session::new(SessionConfig::default(), Some(cache));
    session
        .connect(credentials, false)
        .await
        .map_err(|e| format!("failed to connect to Spotify with database credentials: {e}"))?;
    Ok(session)
}

fn create_cache(cache_dir: &Path) -> Result<Cache, String> {
    std::fs::create_dir_all(cache_dir).map_err(|e| {
        format!(
            "failed to create Spotify cache directory {}: {e}",
            cache_dir.display()
        )
    })?;
    let volume_dir = cache_dir.to_path_buf();
    let audio_dir = cache_dir.join("audio");
    Cache::new(
        None::<PathBuf>,
        Some(volume_dir),
        Some(audio_dir),
        Some(512 * 1024 * 1024),
    )
    .map_err(|e| format!("failed to open Spotify cache: {e}"))
}

async fn database_client() -> Result<tokio_postgres::Client, String> {
    let raw_url = env::var("DATABASE_URL")
        .map_err(|_| "DATABASE_URL is required for Spotify credentials".to_string())?;
    let url = raw_url.strip_prefix("jdbc:").unwrap_or(&raw_url);
    let mut config: tokio_postgres::Config = url
        .parse()
        .map_err(|e| format!("invalid DATABASE_URL: {e}"))?;
    let (client, connection) = config
        .connect(tokio_postgres::NoTls)
        .await
        .map_err(|e| format!("failed to connect to PostgreSQL: {e}"))?;
    tokio::spawn(async move {
        if let Err(error) = connection.await {
            log::warn!("Spotify PostgreSQL connection ended: {error}");
        }
    });
    Ok(client)
}

async fn load_credentials() -> Result<Credentials, String> {
    let client = database_client().await?;
    let row = client
        .query_opt(
            "SELECT credentials FROM spotify_credentials WHERE id = TRUE",
            &[],
        )
        .await
        .map_err(|e| format!("failed to load Spotify credentials from PostgreSQL: {e}"))?
        .ok_or_else(|| "Spotify credentials missing. Run `./start.sh spotify-auth` first.".to_string())?;
    let value: String = row.get(0);
    serde_json::from_str(&value)
        .map_err(|e| format!("invalid Spotify credentials stored in PostgreSQL: {e}"))
}

async fn store_credentials(credentials: &Credentials) -> Result<(), String> {
    let value = serde_json::to_string(credentials)
        .map_err(|e| format!("failed to serialize Spotify credentials: {e}"))?;
    let client = database_client().await?;
    client
        .execute(
            "INSERT INTO spotify_credentials (id, credentials) VALUES (TRUE, $1) \
             ON CONFLICT (id) DO UPDATE SET credentials = EXCLUDED.credentials, updated_at = NOW()",
            &[&value],
        )
        .await
        .map_err(|e| format!("failed to store Spotify credentials in PostgreSQL: {e}"))?;
    Ok(())
}

async fn delete_credentials() -> Result<(), String> {
    let client = database_client().await?;
    client
        .execute(
            "DELETE FROM spotify_credentials WHERE id = TRUE",
            &[],
        )
        .await
        .map_err(|e| format!("failed to delete Spotify credentials from PostgreSQL: {e}"))?;
    eprintln!("Spotify credentials deleted from PostgreSQL");
    Ok(())
}

fn redirect_uri(host: &str, port: u16) -> String {
    if port == 0 {
        format!("http://{host}/login")
    } else {
        format!("http://{host}:{port}/login")
    }
}

fn get_access_token(
    client_id: &str,
    redirect_uri: &str,
    bind_host: &str,
) -> Result<StandardTokenResponse<EmptyExtraTokenFields, BasicTokenType>, String> {
    let auth_url = AuthUrl::new("https://accounts.spotify.com/authorize".to_string())
        .map_err(|e| format!("invalid Spotify auth URL: {e}"))?;
    let token_url = TokenUrl::new("https://accounts.spotify.com/api/token".to_string())
        .map_err(|e| format!("invalid Spotify token URL: {e}"))?;
    let redirect_url = RedirectUrl::new(redirect_uri.to_string())
        .map_err(|e| format!("invalid Spotify redirect URI {redirect_uri}: {e}"))?;
    let client: BasicClient<
        EndpointSet,
        EndpointNotSet,
        EndpointNotSet,
        EndpointNotSet,
        EndpointSet,
    > = BasicClient::new(ClientId::new(client_id.to_string()))
        .set_auth_uri(auth_url)
        .set_token_uri(token_url)
        .set_redirect_uri(redirect_url);
    let (pkce_challenge, pkce_verifier) = PkceCodeChallenge::new_random_sha256();
    let request_scopes = OAUTH_SCOPES
        .iter()
        .map(|scope| Scope::new((*scope).to_string()));
    let (auth_url, csrf_token) = client
        .authorize_url(CsrfToken::new_random)
        .add_scopes(request_scopes)
        .set_pkce_challenge(pkce_challenge)
        .url();

    println!("Browse to: {auth_url}");
    let code = read_auth_code(redirect_uri, bind_host, csrf_token.secret())?;
    let http_client = reqwest::blocking::Client::new();
    client
        .exchange_code(code)
        .set_pkce_verifier(pkce_verifier)
        .request(&http_client)
        .map_err(|e| format!("failed to exchange Spotify OAuth code: {e}"))
}

fn read_auth_code(
    redirect_uri: &str,
    bind_host: &str,
    expected_state: &str,
) -> Result<AuthorizationCode, String> {
    let redirect = Url::parse(redirect_uri)
        .map_err(|e| format!("invalid redirect URI {redirect_uri}: {e}"))?;
    let port = redirect.port().ok_or_else(|| {
        "OAuth redirect URI must include a port for callback handling".to_string()
    })?;
    let bind_addr: SocketAddr = format!("{bind_host}:{port}")
        .parse()
        .map_err(|e| format!("invalid OAuth bind address {bind_host}:{port}: {e}"))?;
    let listener = TcpListener::bind(bind_addr)
        .map_err(|e| format!("failed to bind OAuth callback listener on {bind_addr}: {e}"))?;
    eprintln!("Spotify OAuth callback listening on {bind_addr}");

    let mut stream = listener
        .incoming()
        .flatten()
        .next()
        .ok_or_else(|| "OAuth listener stopped before receiving callback".to_string())?;
    let mut request_line = String::new();
    BufReader::new(&stream)
        .read_line(&mut request_line)
        .map_err(|e| format!("failed to read OAuth callback request: {e}"))?;
    let path = request_line
        .split_whitespace()
        .nth(1)
        .ok_or_else(|| "failed to parse OAuth callback request".to_string())?;
    let callback = Url::parse(&format!("http://localhost{path}"))
        .map_err(|e| format!("failed to parse OAuth callback URL: {e}"))?;
    let params: HashMap<_, _> = callback.query_pairs().into_owned().collect();
    let state = params
        .get("state")
        .ok_or_else(|| "Spotify OAuth callback is missing state".to_string())?;
    if state != expected_state {
        return Err("Spotify OAuth callback state did not match".to_string());
    }
    let code = params
        .get("code")
        .ok_or_else(|| "Spotify OAuth callback is missing code".to_string())?
        .to_string();
    let response_body = "Go back to your terminal :)\n";
    let response = format!(
        "HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{}",
        response_body.len(),
        response_body
    );
    stream
        .write_all(response.as_bytes())
        .map_err(|e| format!("failed to send OAuth callback response: {e}"))?;
    Ok(AuthorizationCode::new(code))
}

fn bitrate(value: u16) -> Result<Bitrate, String> {
    match value {
        96 => Ok(Bitrate::Bitrate96),
        160 => Ok(Bitrate::Bitrate160),
        320 => Ok(Bitrate::Bitrate320),
        _ => Err("Spotify bitrate must be one of 96, 160, 320".to_string()),
    }
}

fn artist_names(item: &AudioItem) -> Vec<String> {
    match &item.unique_fields {
        UniqueFields::Track { artists, .. } => {
            artists.iter().map(|artist| artist.name.clone()).collect()
        }
        UniqueFields::Episode { show_name, .. } => vec![show_name.clone()],
        UniqueFields::Local { artists, .. } => artists.iter().cloned().collect(),
    }
}

fn canonical_track_uri(uri: &SpotifyUri) -> Result<String, String> {
    match uri {
        SpotifyUri::Track { .. } => Ok(uri.to_uri()),
        _ => Err("only Spotify tracks are supported".to_string()),
    }
}

fn parse_track_uri(input: &str) -> Result<SpotifyUri, String> {
    let parsed = parse_spotify_uri(input)?;
    match parsed {
        SpotifyUri::Track { .. } => Ok(parsed),
        _ => Err("only Spotify tracks are supported".to_string()),
    }
}

fn parse_spotify_uri(input: &str) -> Result<SpotifyUri, String> {
    let trimmed = input.trim().trim_matches(['<', '>']);
    let uri = if trimmed.starts_with("spotify:") {
        trimmed.to_string()
    } else {
        spotify_url_to_uri(trimmed)?
    };
    let parsed = SpotifyUri::from_uri(&uri).map_err(|e| format!("invalid Spotify URI: {e}"))?;
    match parsed {
        SpotifyUri::Track { .. } | SpotifyUri::Album { .. } | SpotifyUri::Playlist { .. } => {
            Ok(parsed)
        }
        _ => Err("only Spotify tracks, albums, and playlists are supported".to_string()),
    }
}

fn spotify_url_to_uri(input: &str) -> Result<String, String> {
    let url = Url::parse(input).map_err(|_| "invalid Spotify URL".to_string())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str() != Some("open.spotify.com") {
        return Err("only open.spotify.com URLs are supported".to_string());
    }

    let mut segments: Vec<&str> = url
        .path_segments()
        .ok_or_else(|| "invalid Spotify URL path".to_string())?
        .filter(|segment| !segment.is_empty())
        .collect();
    if segments
        .first()
        .is_some_and(|segment| segment.starts_with("intl-"))
    {
        segments.remove(0);
    }
    if segments.len() != 2 {
        return Err("invalid Spotify item URL".to_string());
    }
    let item_type = segments[0];
    if !matches!(item_type, "track" | "album" | "playlist") {
        return Err("only Spotify tracks, albums, and playlists are supported".to_string());
    }
    let id = segments[1];
    if id.len() != 22 || !id.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err("invalid Spotify item ID".to_string());
    }
    Ok(format!("spotify:{item_type}:{id}"))
}

#[derive(Debug)]
struct Args {
    command: String,
    cache: PathBuf,
    uri: Option<String>,
    bitrate: u16,
    oauth_port: u16,
    redirect_host: String,
    bind_host: String,
}

impl Args {
    fn parse(values: Vec<String>) -> Result<Self, String> {
        let mut iter = values.into_iter();
        let command = iter.next().ok_or_else(usage)?;
        let mut args = Args {
            command,
            cache: PathBuf::from("spotify"),
            uri: None,
            bitrate: 320,
            oauth_port: 0,
            redirect_host: "127.0.0.1".to_string(),
            bind_host: "127.0.0.1".to_string(),
        };

        while let Some(arg) = iter.next() {
            match arg.as_str() {
                "--cache" => args.cache = PathBuf::from(next_value(&mut iter, "--cache")?),
                "--uri" => args.uri = Some(next_value(&mut iter, "--uri")?),
                "--bitrate" => {
                    args.bitrate = next_value(&mut iter, "--bitrate")?
                        .parse()
                        .map_err(|_| "--bitrate must be a number".to_string())?;
                }
                "--oauth-port" => {
                    args.oauth_port = next_value(&mut iter, "--oauth-port")?
                        .parse()
                        .map_err(|_| "--oauth-port must be a number".to_string())?;
                }
                "--redirect-host" => args.redirect_host = next_value(&mut iter, "--redirect-host")?,
                "--bind-host" => args.bind_host = next_value(&mut iter, "--bind-host")?,
                "--help" | "-h" => return Err(usage()),
                _ => return Err(format!("unknown argument: {arg}\n{}", usage())),
            }
        }

        Ok(args)
    }
}

fn next_value(iter: &mut impl Iterator<Item = String>, name: &str) -> Result<String, String> {
    iter.next()
        .ok_or_else(|| format!("{name} requires a value"))
}

fn usage() -> String {
    "usage: spotify-streamer <auth|resolve|stream|credentials-check|credentials-delete> --cache <dir> [--uri <spotify-track-album-or-playlist-url-or-uri>] [--bitrate 96|160|320] [--oauth-port <port>] [--redirect-host <host>] [--bind-host <host>]".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_spotify_track_uri() {
        let uri = parse_track_uri("spotify:track:6rqhFgbbKwnb9MLmUQDhG6").unwrap();
        assert_eq!(uri.to_uri(), "spotify:track:6rqhFgbbKwnb9MLmUQDhG6");
    }

    #[test]
    fn parses_spotify_track_url() {
        let uri = parse_track_uri("https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=abc")
            .unwrap();
        assert_eq!(uri.to_uri(), "spotify:track:6rqhFgbbKwnb9MLmUQDhG6");
    }

    #[test]
    fn parses_spotify_album_and_playlist_items() {
        let album = parse_spotify_uri("spotify:album:4LH4d3cOWNNsVw41Gqt2kv").unwrap();
        assert_eq!(album.to_uri(), "spotify:album:4LH4d3cOWNNsVw41Gqt2kv");

        let playlist =
            parse_spotify_uri("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc")
                .unwrap();
        assert_eq!(playlist.to_uri(), "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M");
    }

    #[test]
    fn parses_locale_prefixed_spotify_url() {
        let album =
            parse_spotify_uri("https://open.spotify.com/intl-de/album/4LH4d3cOWNNsVw41Gqt2kv")
                .unwrap();
        assert_eq!(album.to_uri(), "spotify:album:4LH4d3cOWNNsVw41Gqt2kv");
    }

    #[test]
    fn rejects_unsupported_and_malformed_spotify_items() {
        assert!(
            parse_spotify_uri("https://open.spotify.com/artist/0OdUWJ0sBjDrqHygGUXeCF").is_err()
        );
        assert!(parse_spotify_uri("https://example.com/album/4LH4d3cOWNNsVw41Gqt2kv").is_err());
        assert!(parse_spotify_uri("https://open.spotify.com/album/short").is_err());
        assert!(parse_track_uri("spotify:album:4LH4d3cOWNNsVw41Gqt2kv").is_err());
    }

    #[test]
    fn builds_loopback_redirect_uri_with_port() {
        assert_eq!(
            redirect_uri("127.0.0.1", 45768),
            "http://127.0.0.1:45768/login"
        );
    }

    #[test]
    fn parses_oauth_listener_arguments() {
        let args = Args::parse(vec![
            "auth".to_string(),
            "--cache".to_string(),
            "/musicbot/spotify".to_string(),
            "--oauth-port".to_string(),
            "45768".to_string(),
            "--redirect-host".to_string(),
            "127.0.0.1".to_string(),
            "--bind-host".to_string(),
            "0.0.0.0".to_string(),
        ])
        .unwrap();

        assert_eq!(args.command, "auth");
        assert_eq!(args.cache, PathBuf::from("/musicbot/spotify"));
        assert_eq!(args.oauth_port, 45768);
        assert_eq!(args.redirect_host, "127.0.0.1");
        assert_eq!(args.bind_host, "0.0.0.0");
    }
}
