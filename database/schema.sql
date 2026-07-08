create table if not exists collection_runs (
    id bigserial primary key,
    source text not null,
    city_or_region text not null,
    checkin_date date not null,
    checkout_date date not null,
    number_of_nights integer not null,
    adults integer not null,
    currency text not null,
    max_hotels integer not null,
    started_at timestamp not null default now(),
    completed_at timestamp null,
    status text not null,
    error_message text null,
    excel_file_path text null,
    selected_star_ratings text null,
    include_unknown_star_rating boolean null
);

alter table collection_runs
    add column if not exists selected_star_ratings text;

alter table collection_runs
    add column if not exists include_unknown_star_rating boolean;

create table if not exists hotel_price_results (
    id bigserial primary key,
    collection_run_id bigint not null references collection_runs(id) on delete cascade,
    source text not null,
    city_or_region text not null,
    search_url text null,
    hotel_name text null,
    raw_hotel_name text null,
    property_type_guess text null,
    excluded_by_hotels_only_filter boolean null,
    ota_hotel_id text null,
    star_rating numeric null,
    raw_star_signal text null,
    star_aria_label text null,
    star_icon_count integer null,
    star_rating_missing_reason text null,
    review_score numeric null,
    review_count integer null,
    room_name text null,
    cheapest_room_name text null,
    cheapest_price_total numeric null,
    currency text null,
    taxes_and_fees_text text null,
    checkin_date date not null,
    checkout_date date not null,
    number_of_nights integer not null,
    adults integer not null,
    hotel_url text null,
    provider_name text null,
    raw_source_payload text null,
    ranking_position_on_page integer null,
    screenshot_path text null,
    collection_status text not null,
    error_message text null,
    collected_at timestamp not null default now()
);

create index if not exists idx_hotel_price_results_collection_run_id
    on hotel_price_results(collection_run_id);

alter table hotel_price_results
    add column if not exists raw_hotel_name text;

alter table hotel_price_results
    add column if not exists property_type_guess text;

alter table hotel_price_results
    add column if not exists excluded_by_hotels_only_filter boolean;

alter table hotel_price_results
    add column if not exists raw_star_signal text;

alter table hotel_price_results
    add column if not exists star_aria_label text;

alter table hotel_price_results
    add column if not exists star_icon_count integer;

alter table hotel_price_results
    add column if not exists star_rating_missing_reason text;

alter table hotel_price_results
    add column if not exists room_name text;

alter table hotel_price_results
    add column if not exists provider_name text;

alter table hotel_price_results
    add column if not exists raw_source_payload text;

create index if not exists idx_hotel_price_results_source
    on hotel_price_results(source);

create index if not exists idx_hotel_price_results_city_or_region
    on hotel_price_results(city_or_region);

create index if not exists idx_hotel_price_results_checkin_date
    on hotel_price_results(checkin_date);

create index if not exists idx_hotel_price_results_collected_at
    on hotel_price_results(collected_at);

create index if not exists idx_hotel_price_results_hotel_name
    on hotel_price_results(hotel_name);
