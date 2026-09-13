L.tileLayer("{{ osm_tile_url|escapejs }}", {
    maxZoom: 19,
    attribution: '{{ osm_tile_attribution|safe }}'
})
